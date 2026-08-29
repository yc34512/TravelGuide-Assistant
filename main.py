"""旅游攻略助手 CLI（第一阶段竖切：仅抖音）。

用法：
    python main.py 西湖 --no-llm        # 只采集原始数据，存 JSON（首次建议先跑这个）
    python main.py 西湖                 # 采集 + 生成攻略报告（需先配置 .env 里的 LLM Key）
"""
import argparse
import json
import sys
from datetime import datetime

from rich.console import Console

from config import (
    DEBUG_DIR,
    MAX_COMMENTS_PER_VIDEO,
    MAX_VIDEOS_PER_RUN,
    RAW_DIR,
    REPORT_DIR,
)
from crawler.browser import create_page, ensure_login
from crawler.douyin import DouyinCrawler
from core.rate_limiter import RateLimiter

console = Console()


def dump_debug(page, name: str) -> None:
    """页面处理失败时保存 HTML 便于排查选择器问题。"""
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.html, encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="旅游攻略助手：抖音内容采集 + AI 攻略整合")
    ap.add_argument("keyword", help="景点或主题关键词，如：西湖")
    ap.add_argument("--limit", type=int, default=MAX_VIDEOS_PER_RUN, help="采集视频数上限")
    ap.add_argument("--comments", type=int, default=MAX_COMMENTS_PER_VIDEO, help="每条视频评论数上限")
    ap.add_argument("--no-llm", action="store_true", help="只采集原始数据，不调用 LLM")
    args = ap.parse_args()

    # 开源安全设计：生成报告前确保 API Key 已配置（向导式配置，Key 存系统凭据管理器）
    if not args.no_llm:
        from core.credentials import get_llm_config, interactive_setup

        if get_llm_config() is None and interactive_setup() is None:
            console.print("[red]未完成 API Key 配置，本次按 --no-llm 模式只采集数据。[/red]")
            args.no_llm = True

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    page = create_page()
    crawler = DouyinCrawler(page, RateLimiter())
    items = []
    try:
        if not ensure_login(page):
            console.print("[red]等待超时：未检测到登录态。请重跑本命令，并在窗口中用小号完成扫码。[/red]")
            sys.exit(1)

        console.print(f"[cyan]1/3[/cyan] 搜索关键词：{args.keyword}")
        urls = crawler.search(args.keyword, args.limit)
        console.print(f"    搜到 {len(urls)} 条视频")
        if not urls:
            console.print(
                "[yellow]没抓到结果：可能是页面改版或未登录，"
                "请检查 data/debug/ 下的页面快照，并对照 crawler/douyin.py 顶部选择器。[/yellow]"
            )

        console.print(f"[cyan]2/3[/cyan] 逐条读取视频内容与评论（每条约 20~40 秒，共 {len(urls)} 条）")
        for i, url in enumerate(urls, 1):
            try:
                item = crawler.fetch_video(url, max_comments=args.comments)
                items.append(item)
                console.print(
                    f"    [{i}/{len(urls)}] {item.video_id} | 文案 {len(item.description)} 字 | 评论 {len(item.comments)} 条"
                )
            except Exception as e:
                console.print(f"    [red][{i}/{len(urls)}] 处理失败：{e}（已存调试快照）[/red]")
                dump_debug(page, f"fail_{ts}_{i}")

        raw_path = RAW_DIR / f"{args.keyword}_{ts}.json"
        raw_path.write_text(
            json.dumps([it.to_dict() for it in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"    原始数据已保存：{raw_path}")

        if args.no_llm:
            console.print(f"[green]✔ 采集完成（--no-llm 模式）：{raw_path}[/green]")
            return

        console.print("[cyan]3/3[/cyan] LLM 提取要点并生成报告…")
        from pipeline.extract import extract_points
        from pipeline.render import render_report
        from pipeline.report import synthesize_report
        from pipeline.verify import annotate_confidence

        all_points = []
        for it in items:
            try:
                pts = extract_points(it)
                all_points.extend(pts)
                console.print(f"    {it.video_id}: 提取 {len(pts)} 条要点")
            except Exception as e:
                console.print(f"    [red]{it.video_id} 提取失败：{e}[/red]")

        if not all_points:
            console.print("[yellow]没有提取到任何要点，跳过报告生成。[/yellow]")
            return

        console.print("    多源交叉验证与置信度标注…")
        all_points = annotate_confidence(all_points)

        body = synthesize_report(args.keyword, all_points)
        report_path = REPORT_DIR / f"{args.keyword}_{ts}.md"
        report_path.write_text(
            render_report(args.keyword, body, items, all_points), encoding="utf-8"
        )
        console.print(f"[green]✔ 攻略报告已生成：{report_path}[/green]")

    except KeyboardInterrupt:
        console.print("\n[yellow]已手动中断。[/yellow]")
    finally:
        try:
            page.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
