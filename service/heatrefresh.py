"""热度刷榜任务编排：圈定城市景点 -> 元数据模式轻量采集 -> 热度快照落库。

刷榜走"进详情页取元数据、不采评论"的轻量路径（单条约 10~15 秒），
成本闸：每次最多 6 个景点 × 4 条视频 = 24 次元数据访问，
全程受频控与全局采集锁约束（与攻略/行程任务排队共享浏览器）。
产出写入知识库 heat_snapshots 表，榜单页直接读。
"""
import threading
import time
import uuid
from datetime import datetime

from core import knowledge
from pipeline.candidates import candidate_foods
from pipeline.heat import heat_index, sentiment_trend, time_windows, trend_of
from pipeline.planner import candidate_spots
from service.research import JOBS, _CRAWL_LOCK, _LOCK, Cancelled, dump_debug

HEAT_SPOT_LIMIT = 6       # 每次刷榜的景点数硬上限（成本闸）
HEAT_VIDEOS_PER_SPOT = 4  # 每个景点采的元数据视频数
HEAT_FOOD_LIMIT = 2       # 美食榜并列：每次刷榜额外采的美食/餐厅数（成本闸）
HEAT_SENTIMENT_VIDEOS = 1  # 每个对象采评论的视频数（仅首个，算情感趋势；其余纯元数据控成本）


def start_heat_refresh(city: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": "heat",
            "keyword": f"{city} 热度榜",
            "mode": "heat",
            "status": "running",
            "stage": "排队中",
            "log": [],
            "result": None,
            "error": None,
            "cancel_requested": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(target=_run_heat, args=(job_id, city), daemon=True).start()
    return job_id


def _run_heat(job_id: str, city: str) -> None:
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
        # 1) 圈定刷榜对象：已登记的城市景点优先，兜底 LLM 圈定（联网能力随候选链生效）
        job["stage"] = "圈定对象"
        spots = knowledge.list_city_spots(city)[:HEAT_SPOT_LIMIT]
        if spots:
            log(f"复用已登记的 {len(spots)} 个景点：{'、'.join(spots)}")
        else:
            spots = candidate_spots(city, 1, HEAT_SPOT_LIMIT)
            if not spots:
                raise RuntimeError("景点圈定失败：请换更明确的城市名")
            log(f"自动圈定 {len(spots)} 个景点：{'、'.join(spots)}")
        knowledge.register_city_spots(city, spots)
        # 美食榜并列：额外圈定少量代表性美食/餐厅（失败不阻断景点榜）
        foods: list[str] = []
        try:
            foods = candidate_foods(city, HEAT_FOOD_LIMIT)
            if foods:
                log(f"美食榜刷榜对象：{'、'.join(foods)}")
        except Exception as e:
            log(f"美食候选圈定失败（{e}），本轮只刷景点榜")
        kinds = {s: "景点" for s in spots}
        kinds.update({f: "美食" for f in foods})
        targets = list(spots) + list(foods)

        # 2) 元数据模式采集：单个浏览器会话跑完全部对象（不采评论）
        job["stage"] = "采集热度数据"
        collected: dict[str, list] = {}
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with _CRAWL_LOCK:
            if _cancelled():
                raise Cancelled()
            from core.rate_limiter import RateLimiter
            from crawler.browser import create_page, ensure_login
            from crawler.douyin import DouyinCrawler

            page = create_page()
            crawler = DouyinCrawler(page, RateLimiter())
            try:
                if not ensure_login(page):
                    raise RuntimeError("未检测到抖音登录态：请在弹出的浏览器中用采集小号扫码后重试")
                for si, spot in enumerate(targets, 1):
                    if _cancelled():
                        raise Cancelled()
                    log(f"[{si}/{len(targets)}] 搜索：{spot}（{kinds[spot]}）")
                    try:
                        found = crawler._search_one(spot, HEAT_VIDEOS_PER_SPOT)
                    except Exception as e:
                        log(f"  搜索失败：{e}")
                        continue
                    urls = [c["url"] for c in found[:HEAT_VIDEOS_PER_SPOT]]
                    if not urls:
                        dump_debug(page, f"heat_fail_{ts}_{si}")
                        log("  搜索 0 结果：已存页面快照到 data/debug/")
                        continue
                    items = []
                    for vi, url in enumerate(urls, 1):
                        if _cancelled():
                            raise Cancelled()
                        try:
                            # 首个视频连评论一起采（情感趋势原料），其余只取元数据控成本
                            item = crawler.fetch_video(url, collect_comments=(vi <= HEAT_SENTIMENT_VIDEOS))
                            items.append(item)
                            log(f"  [{vi}/{len(urls)}] {item.video_id} | "
                                f"点赞 {item.like_count or 0} | 发布 {item.publish_time or '未知'}")
                        except Exception as e:
                            log(f"  [{vi}/{len(urls)}] 元数据采集失败：{e}")
                    if items:
                        collected[spot] = items
            finally:
                try:
                    page.quit()
                except Exception:
                    pass

        # 3) 热度画像 + 趋势判定 + 快照落库（景点榜与美食榜并列存储）
        job["stage"] = "更新榜单"
        if not collected:
            raise RuntimeError("未采集到任何热度数据（可能登录态失效或景点过冷）")
        for spot, items in collected.items():
            h = heat_index(items)
            w = time_windows(items)
            trend = trend_of(w["fresh7"], w["old60"], h["score"])
            senti = sentiment_trend([c for it in items for c in it.comments])
            knowledge.upsert_heat_snapshot(city, spot, {
                "score": h["score"], "fresh7": w["fresh7"], "fresh60": w["fresh60"],
                "old60": w["old60"], "likes": h["likes"], "videos": h["videos"],
                "trend": trend, "mkt_ratio": h["mkt_ratio"], "sentiment": senti["trend"],
            }, kind=kinds.get(spot, "景点"))
            warn = "｜⚠️营销号占比高，热度需谨慎看待" if h["mkt_ratio"] >= 0.5 else ""
            log(f"{spot}：热度 {h['score']:.2f} · {trend}"
                f"（近7天 {w['fresh7']:.0%} / 60天以上 {w['old60']:.0%}）"
                f"｜营销号 {h['marketing']}/{h['videos']}｜情感 {senti['trend']}{warn}")

        ranking = knowledge.load_heat_snapshots(city)
        job["result"] = {
            "city": city,
            # 与 /api/heat/{city} 同结构：景点榜与美食榜并列，网页轮询收口直接渲染
            "ranking": [r for r in ranking if r.get("kind") != "美食"],
            "food_ranking": [r for r in ranking if r.get("kind") == "美食"],
            "cache_hit": False,
            "stats": {
                "spots": len(collected),
                "videos": sum(len(v) for v in collected.values()),
                "elapsed": round(time.time() - started, 1),
            },
        }
        _finish("done", "完成")
    except Cancelled:
        log("任务已取消")
        _finish("cancelled", "已取消")
    except Exception as e:
        _finish("error", "失败", str(e))
