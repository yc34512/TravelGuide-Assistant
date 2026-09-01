"""行程规划任务编排：圈定景点 -> 逐点调研 -> 景点档案 -> 通行矩阵 -> 规划生成 -> 渲染。

P0 升级：混合候选验证（大模型圈定 -> 抖音验证采集 -> 交叉验证筛选）、
营销号过滤、热度榜、避坑专题（附评论原文引用）、预算控制、HTML 可视化输出。
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
from pipeline.candidates import (
    VERIFY_MAX,
    candidate_foods,
    generate_candidates,
    is_marketing,
    verify_candidates,
)
from pipeline.extract import extract_points
from pipeline.heat import heat_index, pitfall_digest
from pipeline.planner import (
    build_budget_summary,
    build_review_digest,
    build_spot_profile,
    candidate_spots,
    empty_digest,
    empty_profile,
    plan_itinerary,
    render_trip,
    render_trip_html,
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

TRIP_SPOT_LIMIT = 5      # 逐点调研按 fast 档（候选验证采集同样适用，成本闸）
TRIP_SPOT_COMMENTS = 100
MAX_SPOTS_PER_DAY = 3    # 候选景点上限 = 天数 × 3（简单路径）
MIN_USABLE_SPOTS = 2     # 低于此数的可用调研结果无法排行程
TRIP_FOOD_LIMIT = 3      # 餐厅调研数上限（午/晚餐推荐用，成本闸）


def start_trip(city: str, days: int, hotel: str, spots: list[str] | None,
               preferences: str = "", budget: float | None = None,
               preference_mode: str = "均衡") -> str:
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
        args=(job_id, city, days, hotel, spots, preferences, budget, preference_mode),
        daemon=True,
    ).start()
    return job_id


def _run_trip(job_id: str, city: str, days: int, hotel: str,
              user_spots: list[str] | None, preferences: str,
              budget: float | None = None, preference_mode: str = "均衡") -> None:
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
        # 1) 圈定候选：用户指定 > 混合候选验证（未指定清单时）> 简单圈定兜底
        job["stage"] = "圈定景点"
        categories: dict[str, str] = {}
        pre: dict[str, tuple[list, list[dict]]] | None = None
        if user_spots:
            spots = [s.strip() for s in user_spots if s.strip()][:days * MAX_SPOTS_PER_DAY]
            log(f"使用用户指定景点清单：{'、'.join(spots)}")
            food_names: list[str] = []
            try:
                food_names = candidate_foods(city, TRIP_FOOD_LIMIT)
                if food_names:
                    log(f"美食候选：{'、'.join(food_names)}（将单独调研供午/晚餐推荐）")
            except Exception as e:
                log(f"美食候选圈定失败（{e}），行程不含餐厅推荐")
            food_pre: dict[str, tuple[list, list[dict]]] = {}
        else:
            spots = []
            try:
                cands = generate_candidates(city, days, preferences)
                log(f"大模型圈定 {len(cands)} 个候选，开始逐个验证采集（上限 {VERIFY_MAX} 个）")
                verify_cands = cands[:VERIFY_MAX]
                vstats: dict[str, dict] = {}
                researched: dict[str, tuple[list, list[dict]]] = {}
                for ci, cand in enumerate(verify_cands, 1):
                    if _cancelled():
                        raise Cancelled()
                    name = cand["name"]
                    record = knowledge.find_fresh(name, KB_TTL_DAYS)
                    if record:
                        log(f"  验证[{ci}/{len(verify_cands)}] {name}：缓存命中")
                        items = load_items_from_raw(record["raw_path"])
                    else:
                        log(f"  验证[{ci}/{len(verify_cands)}] {name}：现场采集")
                        items = _crawl(name, TRIP_SPOT_LIMIT, TRIP_SPOT_COMMENTS, False, job_id, log)
                        if items:
                            raw_path = save_raw(name, items)
                            knowledge.record_crawl(
                                name, raw_path, len(items), sum(len(x.comments) for x in items)
                            )
                    # 验证统计：营销号占比（文案正则）+ 预提取立场计数 + 评论摘录样本
                    mkt = sum(1 for it in items if is_marketing(it.description)) if items else 0
                    pts: list[dict] = []
                    if items:
                        with ThreadPoolExecutor(max_workers=3) as pool:
                            for fut in as_completed({pool.submit(extract_points, it): it for it in items}):
                                try:
                                    pts.extend(fut.result())
                                except Exception:
                                    pass
                    pos = sum(1 for p in pts if p.get("stance") == "推荐")
                    neg = sum(1 for p in pts if p.get("stance") == "避雷")
                    vstats[name] = {
                        "videos": len(items), "marketing_hits": mkt,
                        "positive": pos, "negative": neg,
                        "sample_quotes": [p.get("quote") for p in pts if p.get("quote")][:3],
                    }
                    researched[name] = (items, pts)
                    categories[name] = cand["category"]
                results = verify_candidates(verify_cands, vstats)
                kept = [r["name"] for r in results if r["verdict"] == "keep" and r["name"] in researched]
                dropped = [r["name"] for r in results if r["verdict"] == "drop"]
                if dropped:
                    log(f"交叉验证淘汰 {len(dropped)} 个：{'、'.join(dropped[:6])}")
                # 景点优先（行程骨架）；美食候选分流去餐厅调研线（不进景点排序）
                kept.sort(key=lambda n: 0 if categories.get(n) == "景点" else 1)
                pre = {n: researched[n] for n in kept if categories.get(n) != "美食"}
                food_names = [n for n in kept if categories.get(n) == "美食"][:TRIP_FOOD_LIMIT]
                food_pre = {n: researched[n] for n in food_names}
                log(f"保留 {len(kept)} 个优质候选（景点/体验 {len(pre)} + 餐厅 {len(food_pre)}）：{'、'.join(kept)}")
            except Cancelled:
                raise
            except Exception as e:
                log(f"混合候选验证失败（{e}），降级为简单圈定")
                pre = None
            if pre:
                spots = list(pre.keys())
            else:
                food_names = []
                food_pre = {}
                max_n = days * MAX_SPOTS_PER_DAY
                log(f"自动圈定候选景点（上限 {max_n} 个）…")
                spots = candidate_spots(city, days, max_n)
                if not spots:
                    raise RuntimeError("候选景点圈定失败：请换更明确的城市名或直接指定景点清单")
                log(f"候选景点：{'、'.join(spots)}")
        if _cancelled():
            raise Cancelled()
        # 城市景点关联登记入知识库：供热度刷榜优先复用，免去重新圈定
        knowledge.register_city_spots(city, spots)

        # 2) 逐点调研：景点与餐厅同一套调研管道（缓存快路 / fast 档采集，受全局采集锁排队保护）；
        #    混合候选路径直接复用验证采集结果（避免重复 LLM 提取）
        job["stage"] = "调研景点"

        def _research(names: list[str], pre_seeded: dict, label: str):
            """通用调研：复用验证结果 -> 缓存快路 -> 现场采集 -> 要点提取。"""
            points: dict[str, list[dict]] = {}
            sources: dict[str, list[str]] = {}
            items_by: dict[str, list] = {}
            for n, (its, pts0) in pre_seeded.items():
                if pts0:
                    points[n] = pts0
                    sources[n] = [it.url for it in its]
                    items_by[n] = its
            for i, name in enumerate(names, 1):
                if _cancelled():
                    raise Cancelled()
                if name in points:
                    log(f"[{label}{i}/{len(names)}] {name}：复用验证采集结果（{len(points[name])} 条要点）")
                    continue
                record = knowledge.find_fresh(name, KB_TTL_DAYS)
                if record:
                    log(f"[{label}{i}/{len(names)}] {name}：知识库命中，免采集")
                    items = load_items_from_raw(record["raw_path"])
                else:
                    log(f"[{label}{i}/{len(names)}] {name}：未命中，开始采集（fast 档）")
                    items = _crawl(name, TRIP_SPOT_LIMIT, TRIP_SPOT_COMMENTS, False, job_id, log)
                    if items:
                        raw_path = save_raw(name, items)
                        knowledge.record_crawl(
                            name, raw_path, len(items), sum(len(x.comments) for x in items)
                        )
                if not items:
                    log(f"[{label}{i}/{len(names)}] {name}：未采集到内容，跳过")
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
                    log(f"[{label}{i}/{len(names)}] {name}：未提取到要点，跳过")
                    continue
                points[name] = pts
                sources[name] = [it.url for it in items]
                items_by[name] = items
                log(f"[{label}{i}/{len(names)}] {name}：提取 {len(pts)} 条要点")
            return points, sources, items_by

        spot_points, spot_sources, spot_items = _research(spots, pre or {}, "景点 ")
        if len(spot_points) < MIN_USABLE_SPOTS:
            raise RuntimeError(
                f"可用调研结果的景点不足 {MIN_USABLE_SPOTS} 个，无法排行程"
                "（可稍后重试或在请求中直接指定景点清单）"
            )
        food_points, food_sources, food_items = _research(food_names, food_pre, "餐厅 ")
        if food_points:
            log(f"餐厅调研完成：{len(food_points)}/{len(food_names)} 家可用于午/晚餐推荐")
        else:
            log("无可用餐厅调研结果，行程不含餐厅推荐")
        all_items_for_heat = {**spot_items, **food_items}

        # 3) 档案 + 真实评价摘要：景点与餐厅同等待遇（同池 3 路并发）
        job["stage"] = "构建档案"
        profiles: dict[str, dict] = {}
        food_profiles: dict[str, dict] = {}
        digests: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for s, pts in spot_points.items():
                futures[pool.submit(build_spot_profile, s, pts)] = ("profile", s)
                futures[pool.submit(build_review_digest, s, pts)] = ("digest", s)
            for s, pts in food_points.items():
                futures[pool.submit(build_spot_profile, s, pts)] = ("food_profile", s)
                futures[pool.submit(build_review_digest, s, pts)] = ("digest", s)
            for fut in as_completed(futures):
                kind, s = futures[fut]
                try:
                    res = fut.result()
                    if kind == "profile":
                        profiles[s] = res
                    elif kind == "food_profile":
                        food_profiles[s] = res
                    else:
                        digests[s] = res
                except Exception as e:
                    if kind == "profile":
                        log(f"{s}：档案构建失败（{e}），使用空档案兜底")
                        profiles[s] = empty_profile()
                    elif kind == "food_profile":
                        log(f"{s}：餐厅档案失败（{e}），使用空档案兜底")
                        food_profiles[s] = empty_profile()
                    else:
                        log(f"{s}：评价摘要失败（{e}），跳过")
                        digests[s] = empty_digest()
        if _cancelled():
            raise Cancelled()

        # 4) 通行矩阵：高德真实耗时；无 Key 降级（功能可用，顺路精度弱）
        job["stage"] = "计算路线"
        travel_lines: list[str] = []
        locs: dict[str, str] = {}
        if geo.available():
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

        # 5) 规划生成（预算约束 + 每日午/晚餐餐厅推荐）+ 数据分析（预算/避坑/热度）
        job["stage"] = "生成规划"
        plan = plan_itinerary(city, days, hotel, profiles, travel_lines, preferences,
                              budget, preference_mode, foods=food_profiles or None)
        if not plan["days"]:
            raise RuntimeError("规划生成失败：未产出有效行程，请重试或减少天数/景点")

        # 预算：餐厅档案的"餐饮人均"一并计入人均基准（门票类餐厅通常没有）
        budget_summary = build_budget_summary({**profiles, **food_profiles}, plan, days, budget)
        # 避坑：景点与餐厅的避雷要点同榜（每条附评论原文）
        all_points = [p for pts in spot_points.values() for p in pts] \
            + [p for pts in food_points.values() for p in pts]
        pitfall = pitfall_digest(all_points)
        heat_rows: list[dict] = []
        for s, items in all_items_for_heat.items():
            if items:
                h = heat_index(items)
                h["spot"] = s
                heat_rows.append(h)
        heat_rows.sort(key=lambda r: r["score"], reverse=True)
        log(f"预算明细：预估 {budget_summary['total']:.0f} 元"
            + (f"（用户预算 {budget_summary['total_budget']:.0f}，{budget_summary['status']}）" if budget else "（未设预算）")
            + f"；避坑 {len(pitfall)} 条；热度榜 {len(heat_rows)} 个")
        if _cancelled():
            raise Cancelled()

        # 6) 渲染落盘：Markdown + HTML 可视化双输出（共享同一时间戳文件名）
        job["stage"] = "渲染路书"
        ts = datetime.now()
        md = render_trip(city, days, hotel, plan, profiles, spot_sources, geo.available(),
                         budget_summary=budget_summary, pitfall=pitfall, heat=heat_rows,
                         digests=digests, foods=food_profiles or None,
                         food_sources=food_sources or None)
        report_path = REPORT_DIR / f"行程_{city}_{ts:%Y%m%d_%H%M%S}.md"
        report_path.write_text(md, encoding="utf-8")
        # 行程报告无采集档案，单独登记进报告表，网页历史列表才不会遗漏
        knowledge.register_report(
            f"行程·{city}{days}天", str(report_path),
            video_count=sum(len(v) for v in all_items_for_heat.values()),
            comment_count=sum(len(it.comments) for items in all_items_for_heat.values() for it in items),
        )
        html_path = report_path.with_suffix(".html")
        try:
            html_path.write_text(
                render_trip_html(city, days, hotel, plan, profiles, spot_sources,
                                 geo.available(), locs=locs, budget_summary=budget_summary,
                                 pitfall=pitfall, heat=heat_rows, digests=digests,
                                 foods=food_profiles or None, food_sources=food_sources or None),
                encoding="utf-8",
            )
            log(f"行程已保存：{report_path.name}（含 HTML 可视化版 {html_path.name}）")
        except Exception as e:
            html_path = None
            log(f"HTML 渲染失败（不影响 Markdown 结果）：{e}")

        job["result"] = {
            "report_path": str(report_path),
            "report_name": report_path.name,
            "html_name": html_path.name if html_path else None,
            "markdown": md,
            "cache_hit": False,
            "budget_summary": budget_summary,
            "pitfall_digest": pitfall,
            "heat_rank": heat_rows,
            "stats": {
                "spots": len(spot_points),
                "foods": len(food_points),
                "days": days,
                "points": sum(len(v) for v in spot_points.values())
                + sum(len(v) for v in food_points.values()),
                "elapsed": round(time.time() - started, 1),
            },
        }
        _finish("done", "完成")
    except Cancelled:
        log("任务已取消")
        _finish("cancelled", "已取消")
    except Exception as e:
        _finish("error", "失败", str(e))
