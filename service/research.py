"""研究任务编排：知识库判定 -> (采集 | 复用缓存) -> 提取 -> 缺口补全 -> 验证 -> 报告。

对外提供 start_job / get_job / cancel_job 三个原语：任务在后台线程执行，
进度写入 JOBS 字典，由 API 层轮询；终态（完成/失败/取消）同步落库，
服务重启后历史任务仍可查询。浏览器采集全局唯一（_CRAWL_LOCK），
避免多个任务同时抢同一个浏览器实例。
"""
import json
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import DEBUG_DIR, KB_TTL_DAYS, RAW_DIR, REPORT_DIR
from core import knowledge
from core.models import Comment, VideoItem

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()        # JOBS 字典读写保护
_CRAWL_LOCK = threading.Lock()  # 浏览器采集全局唯一：同一时刻只允许一个采集任务
FETCH_RETRIES = 2               # 单条视频采集失败自动重试次数（偶发网络/渲染抖动）

# —— 信息缺口补全（合规硬上限：最多 2 次补充搜索，每次最多 3 条视频）——
CORE_TOPICS = ("门票", "交通")  # 攻略核心主题：完全缺失时触发定向补采
MAX_GAP_SEARCHES = 2
GAP_VIDEOS_PER_SEARCH = 3


class Cancelled(RuntimeError):
    """任务被用户取消：在检查点抛出，让 finally 正常释放浏览器等资源。"""
    pass


def dump_debug(page, name: str) -> None:
    """页面处理失败时保存 HTML 快照，便于排查选择器问题。"""
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.html, encoding="utf-8")
    except Exception:
        pass


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
            transcript=d.get("transcript", ""),  # deep 模式的转写成果随原始 JSON 复用
            comments=[Comment(**c) for c in d.get("comments", [])],
        )
        for d in data
    ]


def _crawl(keyword: str, limit: int, comments: int, asr: bool, job_id: str | None, log) -> list[VideoItem]:
    """浏览器采集（搜索择优 + 单条重试）。浏览器阶段持锁：同一时刻只跑一个采集任务；
    转写在锁释放后进行。原始 JSON 落盘由调用方在缺口补全后统一做（含补采视频）。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def cancelled() -> bool:
        if job_id is None:
            return False
        with _LOCK:
            return JOBS.get(job_id, {}).get("cancel_requested", False)

    with _CRAWL_LOCK:
        if cancelled():
            raise Cancelled()
        from crawler.browser import create_page, ensure_login
        from crawler.douyin import DouyinCrawler
        from core.rate_limiter import RateLimiter

        page = create_page()
        crawler = DouyinCrawler(page, RateLimiter())
        items: list[VideoItem] = []
        try:
            if not ensure_login(page):
                raise RuntimeError("未检测到抖音登录态：请在弹出的浏览器中用采集小号扫码后重试")
            log(f"搜索关键词：{keyword}（多角度查询 + 按点赞择优）")
            urls = crawler.search_and_rank(keyword, limit)
            log(f"搜到 {len(urls)} 条视频")
            if not urls:
                # 0 结果大概率是选择器失效：存快照 + 明确告警，降低排查成本
                dump_debug(page, f"svc_fail_{ts}_search")
                log("搜索 0 结果：已保存页面快照到 data/debug/，请对照 crawler/douyin.py 顶部 SEL_* 常量排查")
            for i, url in enumerate(urls, 1):
                if cancelled():
                    raise Cancelled()
                # 单条重试：页面渲染/网络偶发抖动很常见，重试一次成功率明显提升；
                # 重试仍失败才记日志继续下一条，不阻断整体采集。
                last_err: Exception | None = None
                for attempt in range(FETCH_RETRIES + 1):
                    try:
                        item = crawler.fetch_video(url, max_comments=comments, with_asr=asr)
                        items.append(item)
                        msg = (f"[{i}/{len(urls)}] {item.video_id} | 文案 {len(item.description)} 字 | "
                               f"评论 {len(item.comments)} 条")
                        if asr and item.play_urls:
                            msg += f" | 已捕获 {len(item.play_urls)} 个媒体地址"
                        if attempt:
                            msg += f"（第 {attempt + 1} 次尝试成功）"
                        log(msg)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < FETCH_RETRIES:
                            log(f"[{i}/{len(urls)}] 采集失败：{e}，稍后重试…")
                            time.sleep(3)
                if last_err is not None:
                    dump_debug(page, f"svc_fail_{ts}_{i}")
                    log(f"[{i}/{len(urls)}] 采集失败（已重试 {FETCH_RETRIES} 次）：{last_err}")
        finally:
            try:
                page.quit()
            except Exception:
                pass

    # 转写在浏览器释放后并行进行：下载+CPU 推理不占用浏览器，2 路并发
    if asr:
        from core.asr import transcribe_items

        pending = [it for it in items if it.play_urls and not it.transcript]
        if pending:
            log(f"开始口播转写：{len(pending)} 条视频并行处理")
            t0 = time.time()

            def _prog(done, total, it, text):
                log(f"  转写 {done}/{total}：{it.video_id} -> {len(text)} 字")

            transcribe_items(items, workers=2, progress=_prog)
            ok = sum(1 for it in items if it.transcript)
            log(f"转写完成 {ok}/{len(items)} 条，耗时 {time.time() - t0:.0f} 秒")
    return items


def save_raw(keyword: str, items: list[VideoItem]) -> str:
    """把采集结果（含缺口补采）存为原始 JSON，返回路径。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = RAW_DIR / f"{keyword}_{ts}.json"
    raw_path.write_text(
        json.dumps([it.to_dict() for it in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(raw_path)


def missing_topics(points: list[dict]) -> list[str]:
    """核心主题缺口检测：返回要点中完全未覆盖的核心主题。纯函数，独立可测。"""
    covered = {p.get("topic") for p in points}
    return [t for t in CORE_TOPICS if t not in covered]


def _fill_gaps(keyword: str, items: list[VideoItem], all_points: list[dict],
               comments: int, job_id: str | None, log) -> tuple[list[VideoItem], list[dict]]:
    """信息缺口补全：核心主题（门票/交通）完全缺失时，定向补搜补采一轮。
    硬上限 MAX_GAP_SEARCHES 次查询 × 每次 GAP_VIDEOS_PER_SEARCH 条视频，守频控；
    补采失败不阻断主流程（宁缺勿崩）。job_id 为 None 时（CLI）不响应取消。"""
    gaps = missing_topics(all_points)[:MAX_GAP_SEARCHES]
    if not gaps:
        log("核心主题（门票/交通）已覆盖，无需补全")
        return items, all_points
    log(f"检测到信息缺口：{'、'.join(gaps)}，开始定向补采")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def cancelled() -> bool:
        if job_id is None:
            return False
        with _LOCK:
            return JOBS.get(job_id, {}).get("cancel_requested", False)

    new_items: list[VideoItem] = []
    with _CRAWL_LOCK:
        from crawler.browser import create_page, ensure_login
        from crawler.douyin import DouyinCrawler
        from core.rate_limiter import RateLimiter

        page = create_page()
        crawler = DouyinCrawler(page, RateLimiter())
        try:
            if not ensure_login(page):
                log("缺口补采跳过：未检测到登录态")
                return items, all_points
            for gi, topic in enumerate(gaps, 1):
                if cancelled():
                    raise Cancelled()
                q = f"{keyword} {topic}"
                log(f"补采 [{gi}/{len(gaps)}] 搜索：{q}")
                urls = crawler.search_and_rank(q, GAP_VIDEOS_PER_SEARCH)
                if not urls:
                    dump_debug(page, f"gap_fail_{ts}_{gi}")
                for url in urls:
                    if cancelled():
                        raise Cancelled()
                    try:
                        new_items.append(crawler.fetch_video(url, max_comments=comments))
                        log(f"  补采 {new_items[-1].video_id} | 评论 {len(new_items[-1].comments)} 条")
                    except Exception as e:
                        log(f"  补采失败：{e}")
        finally:
            try:
                page.quit()
            except Exception:
                pass

    if not new_items:
        log("缺口补采未获得新内容，跳过")
        return items, all_points

    # 补采视频提取要点（3 路并发，与主提取一致）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from pipeline.extract import extract_points

    extra_points: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(extract_points, it): it for it in new_items}
        for fut in as_completed(futures):
            if cancelled():
                raise Cancelled()
            it = futures[fut]
            try:
                pts = fut.result()
                extra_points.extend(pts)
                log(f"  {it.video_id}: 补采提取 {len(pts)} 条要点")
            except Exception as e:
                log(f"  {it.video_id}: 补采提取失败 {e}")
    log(f"缺口补全完成：新增 {len(new_items)} 条视频 / {len(extra_points)} 条要点")
    return items + new_items, all_points + extra_points


# 模式预设：快速迭代用 fast，出发前做最终规划用 deep
MODES = {
    "fast":     {"limit": 5,  "comments": 30, "asr": False},
    "standard": {"limit": 8,  "comments": 40, "asr": False},
    "deep":     {"limit": 10, "comments": 50, "asr": True},
}


def start_job(keyword: str, mode: str = "standard", force: bool = False) -> str:
    preset = MODES.get(mode, MODES["standard"])
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "keyword": keyword,
            "mode": mode,
            "status": "running",
            "stage": "排队中",
            "log": [],
            "result": None,
            "error": None,
            "cancel_requested": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_job,
        args=(job_id, keyword, preset["limit"], preset["comments"], preset["asr"], force),
        daemon=True,
    ).start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = JOBS.get(job_id)
        if job:
            return dict(job)
    # 内存未命中（服务重启过）：回退查持久化档案，历史任务仍可看
    return knowledge.load_job(job_id)


def cancel_job(job_id: str) -> bool:
    """请求取消运行中的任务。检查点在下条视频/下批提取前生效，
    正在执行的单步会走完；已在终态的任务不可取消。"""
    with _LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "running":
            return False
        job["cancel_requested"] = True
    return True


def _run_job(job_id: str, keyword: str, limit: int, comments: int, asr: bool, force: bool) -> None:
    job = JOBS[job_id]
    started = time.time()

    def log(msg: str) -> None:
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _cancelled() -> bool:
        return job.get("cancel_requested", False)

    def _finish(status: str, stage: str, error: str | None = None) -> None:
        """统一收口：写终态 + 落库，任何分支退出都经过这里。"""
        job["status"] = status
        job["stage"] = stage
        job["error"] = error
        try:
            knowledge.record_job(job)
        except Exception as e:
            log(f"任务档案落库失败（不影响结果）：{e}")

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
            items = _crawl(keyword, limit, comments, asr, job_id, log)
            if not items:
                raise RuntimeError(
                    "没有采集到任何内容：疑似选择器失效或关键词过冷，"
                    "请查看 data/debug/ 快照并对照 crawler/douyin.py 顶部 SEL_* 常量"
                )
            record_id = None  # 缺口补全后再落盘与登记（补采视频一并入库）
            raw_path = None

        # 2) 提取（并行）
        job["stage"] = "提取要点"
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from pipeline.extract import extract_points

        all_points: list[dict] = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(extract_points, it): it for it in items}
            for fut in as_completed(futures):
                if _cancelled():
                    raise Cancelled()
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
        if _cancelled():
            raise Cancelled()

        # 2.5) 信息缺口补全（门票/交通等核心主题完全缺失时定向补采）
        job["stage"] = "补全缺口"
        items, all_points = _fill_gaps(keyword, items, all_points, comments, job_id, log)
        if _cancelled():
            raise Cancelled()

        # 2.6) 缺口补全后统一落盘与登记：补采视频一并入原始 JSON 和知识库计数
        if not job.get("cache_hit"):
            raw_path = save_raw(keyword, items)
            log(f"原始数据已保存：{Path(raw_path).name}")
            record_id = knowledge.record_crawl(
                keyword, raw_path, len(items), sum(len(i.comments) for i in items)
            )

        # 3) 交叉验证（含量化置信度：高/中/低 + 独立来源数，营销号来源降级）
        job["stage"] = "交叉验证"
        from pipeline.candidates import is_marketing
        from pipeline.verify import annotate_confidence

        mkt_src = {it.url for it in items if is_marketing(it.description)}
        all_points = annotate_confidence(all_points, marketing_sources=mkt_src)
        dist = Counter(p["confidence"] for p in all_points)
        lvl = Counter(p.get("conf_level") for p in all_points if p.get("conf_level"))
        log("置信度分布：" + "、".join(f"{k} {v}" for k, v in dist.items())
            + ("｜等级：" + "、".join(f"{k} {v}" for k, v in lvl.items()) if lvl else ""))

        # 4) 报告
        job["stage"] = "生成报告"
        if _cancelled():
            raise Cancelled()
        from pipeline.render import render_report
        from pipeline.report import synthesize_report

        body = synthesize_report(keyword, all_points)
        report_path = REPORT_DIR / f"{keyword}_{datetime.now():%Y%m%d_%H%M%S}.md"
        report_path.write_text(
            render_report(keyword, body, items, all_points), encoding="utf-8"
        )
        knowledge.update_report(record_id, str(report_path))
        log(f"报告已保存：{report_path.name}")

        job["result"] = {
            "report_path": str(report_path),
            "report_name": report_path.name,
            "markdown": report_path.read_text(encoding="utf-8"),
            "cache_hit": job.get("cache_hit", False),
            "stats": {
                "videos": len(items),
                "comments": sum(len(i.comments) for i in items),
                "points": len(all_points),
                "elapsed": round(time.time() - started, 1),
            },
        }
        _finish("done", "完成")
    except Cancelled:
        log("任务已取消")
        _finish("cancelled", "已取消")
    except Exception as e:
        _finish("error", "失败", str(e))
