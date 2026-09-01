"""行程规划任务编排：圈定景点 -> 逐点调研 -> 景点档案 -> 通行矩阵 -> 规划生成 -> 渲染。

复用 research 的任务框架（JOBS/取消/终态落库/历史查询）与采集管道；
行程任务与攻略任务共享 _CRAWL_LOCK（全局只允许一个浏览器采集）。
为控制总时长，行程内的逐点调研用 fast 档且关闭缺口补全与 ASR。
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from config import KB_TTL_DAYS, REPORT_DIR
from core import geo, knowledge
from pipeline.extract import extract_points
from pipeline.planner import (
    build_spot_profile,
    candidate_spots,
    empty_profile,
    plan_itinerary,
    render_trip,
)
from service.research import (
    JOBS,
    _CRAWL_LOCK,
    _LOCK,
    Cancelled,
    _crawl,
    load_items_from_raw,
    save_raw,
)

TRIP_SPOT_LIMIT = 5      # 逐点调研按 fast 档
TRIP_SPOT_COMMENTS = 30
MAX_SPOTS_PER_DAY = 3    # 候选景点上限 = 天数 × 3
MIN_USABLE_SPOTS = 2     # 低于此数的可用调研结果无法排行程


def start_trip(city: str, days: int, hotel: str, spots: list[str] | None,
               preferences: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": "trip",
            "keyword": f"{city} {days}天行程",
            "mode": "trip",
            "status": "running",
            "stage": "排队中",
            "log": [],
            "result": None,
            "error": None,
            "cancel_requested": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_trip,
        args=(job_id, city, days, hotel, spots, preferences),
        daemon=True,
    ).start()
    return job_id


def _run_trip(job_id: str, city: str, days: int, hotel: str,
              user_spots: list[str] | None, preferences: str) -> None:
    job = JOBS[job_id]
    started = time.time()

    def log(msg: str) -> None:
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _cancelled() -> bool:
        return job.get("cancel_requested", False)

    def _finish(status: str, stage: str, error: str | None = None) -> None:
        job["status"] = status
        job["stage"] = stage
        job["error"] = error
        try:
            knowledge.record_job(job)
        except Exception as e:
            log(f"任务档案落库失败（不影响结果）：{e}")

    try:
        # 1) 圈定候选：用户指定优先，否则 LLM 圈定（上限 天数×3）
        job["stage"] = "圈定景点"
        if user_spots:
            spots = [s.strip() for s in user_spots if s.strip()][:days * MAX_SPOTS_PER_DAY]
            log(f"使用用户指定景点清单：{'、'.join(spots)}")
        else:
            max_n = days * MAX_SPOTS_PER_DAY
            log(f"自动圈定候选景点（上限 {max_n} 个）…")
            spots = candidate_spots(city, days, max_n)
            if not spots:
                raise RuntimeError("候选景点圈定失败：请换更明确的城市名或直接指定景点清单")
            log(f"候选景点：{'、'.join(spots)}")
        if _cancelled():
            raise Cancelled()

        # 2) 逐点调研：缓存命中走快路；未命中按 fast 档采集（受全局采集锁排队保护）
        job["stage"] = "调研景点"
        spot_points: dict[str, list[dict]] = {}
        spot_sources: dict[str, list[str]] = {}
        for i, spot in enumerate(spots, 1):
            if _cancelled():
                raise Cancelled()
            record = knowledge.find_fresh(spot, KB_TTL_DAYS)
            if record:
                log(f"[{i}/{len(spots)}] {spot}：知识库命中，免采集")
                items = load_items_from_raw(record["raw_path"])
            else:
                log(f"[{i}/{len(spots)}] {spot}：未命中，开始采集（fast 档）")
                items = _crawl(spot, TRIP_SPOT_LIMIT, TRIP_SPOT_COMMENTS, False, job_id, log)
                if items:
                    raw_path = save_raw(spot, items)
                    knowledge.record_crawl(
                        spot, raw_path, len(items), sum(len(x.comments) for x in items)
                    )
            if not items:
                log(f"[{i}/{len(spots)}] {spot}：未采集到内容，跳过")
                continue
            pts: list[dict] = []
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(extract_points, it): it for it in items}
                for fut in as_completed(futures):
                    if _cancelled():
                        raise Cancelled()
                    try:
                        pts.extend(fut.result())
                    except Exception as e:
                        log(f"  要点提取失败：{e}")
            if not pts:
                log(f"[{i}/{len(spots)}] {spot}：未提取到要点，跳过")
                continue
            spot_points[spot] = pts
            spot_sources[spot] = [it.url for it in items]
            log(f"[{i}/{len(spots)}] {spot}：提取 {len(pts)} 条要点")
        if len(spot_points) < MIN_USABLE_SPOTS:
            raise RuntimeError(
                f"可用调研结果的景点不足 {MIN_USABLE_SPOTS} 个，无法排行程"
                "（可稍后重试或在请求中直接指定景点清单）"
            )

        # 3) 景点档案：每个景点蒸馏一份结构化档案（3 路并发）
        job["stage"] = "构建档案"
        profiles: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(build_spot_profile, s, pts): s for s, pts in spot_points.items()}
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    profiles[s] = fut.result()
                except Exception as e:
                    log(f"{s}：档案构建失败（{e}），使用空档案兜底")
                    profiles[s] = empty_profile()
        if _cancelled():
            raise Cancelled()

        # 4) 通行矩阵：高德真实耗时；无 Key 降级（功能可用，顺路精度弱）
        job["stage"] = "计算路线"
        travel_lines: list[str] = []
        if geo.available():
            locs: dict[str, str] = {}
            if hotel:
                h = geo.geocode_poi(hotel, city)
                if h:
                    locs["酒店"] = h["location"]
                else:
                    log(f"酒店\"{hotel}\"未能在地图定位，行程将以景点间通行排线")
            for s in spot_points:
                g = geo.geocode_poi(s, city)
                if g:
                    locs[s] = g["location"]
                else:
                    log(f"景点\"{s}\"未能在地图定位，相关路段将缺通行数据")
            if "酒店" in locs:
                for s in spot_points:
                    if s in locs:
                        tt = geo.travel_time(locs["酒店"], locs[s], city)
                        if tt:
                            travel_lines.append(f"酒店->{s}: {tt[1]}约{tt[0]}分钟")
            names = [s for s in spot_points if s in locs]
            for a in names:
                for b in names:
                    if a != b:
                        tt = geo.travel_time(locs[a], locs[b], city)
                        if tt:
                            travel_lines.append(f"{a}->{b}: {tt[1]}约{tt[0]}分钟")
            log(f"高德通行矩阵：{len(travel_lines)} 条路段")
        else:
            log("未配置高德 Key（AMAP_API_KEY）：降级为纯 LLM 按区域排线")
        if _cancelled():
            raise Cancelled()

        # 5) 规划生成 + 6) 渲染落盘
        job["stage"] = "生成规划"
        plan = plan_itinerary(city, days, hotel, profiles, travel_lines, preferences)
        if not plan["days"]:
            raise RuntimeError("规划生成失败：未产出有效行程，请重试或减少天数/景点")

        job["stage"] = "渲染路书"
        md = render_trip(city, days, hotel, plan, profiles, spot_sources, geo.available())
        report_path = REPORT_DIR / f"行程_{city}_{datetime.now():%Y%m%d_%H%M%S}.md"
        report_path.write_text(md, encoding="utf-8")
        log(f"行程已保存：{report_path.name}")

        job["result"] = {
            "report_path": str(report_path),
            "report_name": report_path.name,
            "markdown": md,
            "cache_hit": False,
            "stats": {
                "spots": len(spot_points),
                "days": days,
                "points": sum(len(v) for v in spot_points.values()),
                "elapsed": round(time.time() - started, 1),
            },
        }
        _finish("done", "完成")
    except Cancelled:
        log("任务已取消")
        _finish("cancelled", "已取消")
    except Exception as e:
        _finish("error", "失败", str(e))
