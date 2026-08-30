"""研究任务编排：知识库判定 -> (采集 | 复用缓存) -> 提取 -> 验证 -> 报告。

对外提供 start_job / get_job 两个原语：任务在后台线程执行，进度写入
JOBS 字典，由 API 层轮询。浏览器采集全局唯一（_CRAWL_LOCK），避免多个
任务同时抢同一个浏览器实例。
"""
import json
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import (
    ASR_ENABLED,
    KB_TTL_DAYS,
    MAX_COMMENTS_PER_VIDEO,
    MAX_VIDEOS_PER_RUN,
    RAW_DIR,
    REPORT_DIR,
)
from core import knowledge
from core.models import Comment, VideoItem

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()        # JOBS 字典读写保护
_CRAWL_LOCK = threading.Lock()  # 浏览器采集全局唯一：同一时刻只允许一个采集任务


def load_items_from_raw(raw_path: str) -> list[VideoItem]:
    """把知识库/原始文件里的 JSON 还原成统一数据模型。"""
    data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    return [
        VideoItem(
            video_id=d["video_id"],
            url=d["url"],
            description=d.get("description", ""),
            tags=d.get("tags", []),
            like_count=d.get("like_count"),
            publish_time=d.get("publish_time"),
            comments=[Comment(**c) for c in d.get("comments", [])],
        )
        for d in data
    ]


def _crawl(keyword: str, limit: int, comments: int, log) -> tuple[list[VideoItem], str]:
    """浏览器采集。全程持锁：同一时刻只跑一个采集任务。"""
    with _CRAWL_LOCK:
        from crawler.browser import create_page, ensure_login
        from crawler.douyin import DouyinCrawler
        from core.rate_limiter import RateLimiter

        page = create_page()
        crawler = DouyinCrawler(page, RateLimiter())
        items: list[VideoItem] = []
        try:
            if not ensure_login(page):
                raise RuntimeError("未检测到抖音登录态：请在弹出的浏览器中用采集小号扫码后重试")
            log(f"搜索关键词：{keyword}")
            urls = crawler.search(keyword, limit)
            log(f"搜到 {len(urls)} 条视频")
            for i, url in enumerate(urls, 1):
                try:
                    item = crawler.fetch_video(url, max_comments=comments,
                                               with_asr=ASR_ENABLED)
                    items.append(item)
                    msg = (f"[{i}/{len(urls)}] {item.video_id} | 文案 {len(item.description)} 字 | "
                           f"评论 {len(item.comments)} 条")
                    if ASR_ENABLED:
                        msg += f" | 口播转写 {len(item.transcript)} 字" if item.transcript else " | 口播转写不可用"
                    log(msg)
                except Exception as e:
                    log(f"[{i}/{len(urls)}] 采集失败：{e}")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_path = RAW_DIR / f"{keyword}_{ts}.json"
            raw_path.write_text(
                json.dumps([it.to_dict() for it in items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log(f"原始数据已保存：{raw_path.name}")
            return items, str(raw_path)
        finally:
            try:
                page.quit()
            except Exception:
                pass


def start_job(keyword: str, limit: int = MAX_VIDEOS_PER_RUN,
              comments: int = MAX_COMMENTS_PER_VIDEO, force: bool = False) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "keyword": keyword,
            "status": "running",
            "stage": "排队中",
            "log": [],
            "result": None,
            "error": None,
        }
    threading.Thread(
        target=_run_job, args=(job_id, keyword, limit, comments, force), daemon=True
    ).start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _run_job(job_id: str, keyword: str, limit: int, comments: int, force: bool) -> None:
    job = JOBS[job_id]
    started = time.time()

    def log(msg: str) -> None:
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")

    try:
        # 1) 知识库判定
        job["stage"] = "查询知识库"
        record = None if force else knowledge.find_fresh(keyword, KB_TTL_DAYS)
        if record:
            job["cache_hit"] = True
            record_id = record["id"]
            log(f"知识库命中：{record['crawled_at']} 采集（{record['video_count']} 视频/"
                f"{record['comment_count']} 评论），保鲜期内免重爬")
            items = load_items_from_raw(record["raw_path"])
        else:
            job["cache_hit"] = False
            reason = "已过期" if (not force and knowledge.find_fresh(keyword, KB_TTL_DAYS * 365)) else "首次查询"
            log(f"知识库未命中（{reason}），开始采集")
            job["stage"] = "采集数据"
            items, raw_path = _crawl(keyword, limit, comments, log)
            if not items:
                raise RuntimeError("没有采集到任何内容：可能页面改版或关键词过冷，请查看调试快照")
            record_id = knowledge.record_crawl(
                keyword, raw_path, len(items), sum(len(i.comments) for i in items)
            )

        # 2) 提取（并行）
        job["stage"] = "提取要点"
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from pipeline.extract import extract_points

        all_points: list[dict] = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(extract_points, it): it for it in items}
            for fut in as_completed(futures):
                it = futures[fut]
                try:
                    pts = fut.result()
                    all_points.extend(pts)
                    log(f"{it.video_id}: 提取 {len(pts)} 条要点")
                except Exception as e:
                    log(f"{it.video_id}: 提取失败 {e}")
        log(f"提取耗时 {time.time() - t0:.1f} 秒，共 {len(all_points)} 条要点")
        if not all_points:
            raise RuntimeError("没有提取到任何要点")

        # 3) 交叉验证
        job["stage"] = "交叉验证"
        from pipeline.verify import annotate_confidence

        all_points = annotate_confidence(all_points)
        dist = Counter(p["confidence"] for p in all_points)
        log("置信度分布：" + "、".join(f"{k} {v}" for k, v in dist.items()))

        # 4) 报告
        job["stage"] = "生成报告"
        from pipeline.render import render_report
        from pipeline.report import synthesize_report

        body = synthesize_report(keyword, all_points)
        report_path = REPORT_DIR / f"{keyword}_{datetime.now():%Y%m%d_%H%M%S}.md"
        report_path.write_text(
            render_report(keyword, body, items, all_points), encoding="utf-8"
        )
        knowledge.update_report(record_id, str(report_path))
        log(f"报告已保存：{report_path.name}")

        job["status"] = "done"
        job["stage"] = "完成"
        job["result"] = {
            "report_path": str(report_path),
            "markdown": report_path.read_text(encoding="utf-8"),
            "cache_hit": job.get("cache_hit", False),
            "stats": {
                "videos": len(items),
                "comments": sum(len(i.comments) for i in items),
                "points": len(all_points),
                "elapsed": round(time.time() - started, 1),
            },
        }
    except Exception as e:
        job["status"] = "error"
        job["stage"] = "失败"
        job["error"] = str(e)
