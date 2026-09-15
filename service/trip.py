"""行程规划任务编排：圈定景点 -> 逐点调研 -> 景点档案 -> 通行矩阵 -> 规划生成 -> 渲染。

关键链路：混合候选验证（大模型圈定 -> 抖音验证采集 -> 交叉验证筛选）、
营销号过滤、热度榜、避坑专题（附评论原文引用）、HTML 可视化输出。
先读高赞攻略视频的逐日行程编排（视频行程草案）-> LLM 审核增删改 ->
草案点位逐个验证采集 -> 规划以草案为主干。预算估算整体退役（估算金额不可靠，
预算交由用户自行考虑），报告只保留调研到的确定事实价。
复用 research 的任务框架（JOBS/取消/终态落库/历史查询）与采集管道；
行程任务与攻略任务共享 _CRAWL_LOCK（全局只允许一个浏览器采集）。
为控制总时长，行程内的逐点调研用 fast 档且关闭缺口补全与 ASR。
"""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from config import (CITY_GUIDE_ENABLED, CITY_GUIDE_TTL_DAYS, CITY_GUIDE_VIDEOS,
                    KB_TTL_DAYS, REPORT_DIR)
from core import geo, knowledge
from core.llm import usage_note
from core.official_facts import load_city_facts
from pipeline.candidates import (
    VERIFY_MAX,
    candidate_foods,
    draft_spot_names,
    empty_guide_knowledge,
    extract_guide_knowledge,
    generate_candidates,
    is_marketing,
    review_guide_itinerary,
    select_verify_candidates,
    verify_candidates,
)
from pipeline.extract import extract_points
from pipeline.heat import heat_index, pitfall_digest, sentiment_trend
from pipeline.verify import annotate_confidence
from pipeline.decision import (apply_attribution_check, build_decisions, build_trip_plan,
                               spot_evidence)
from pipeline.qc import apply_trip_plan_checks, finalize, run_quality_gate
from pipeline.planner import (
    build_legs,
    build_overview,
    build_review_digest,
    build_spot_profile,
    candidate_spots,
    day_weekdays_from,
    empty_digest,
    empty_profile,
    plan_itinerary,
    transport_hints,
)
from pipeline.trip_render import render_html as render_tp_html, render_markdown as render_tp_md
from crawler.base import SourceDisabled, douyin_enabled
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
TRIP_FOOD_LIMIT = 3      # 餐厅调研数上限（美食推荐榜用，不排入时间线，成本闸）
# 美食榜样本下限（可 env 调高；配额取与本限的较大者，默认不增采集成本）
FOOD_RANK_MIN_SAMPLES = max(1, int(os.getenv("FOOD_SAMPLE_MIN", "3")))
QC_REPAIR_ROUNDS = 1     # 质量门禁不达标时的回炉轮次上限（F5.2；每轮多一次规划 LLM 调用，成本敏感故设 1）


_UGC_WARNED = False


def _persist_heat(city: str, heat_rows: list[dict], items_by_name: dict,
                  food_names: set[str]) -> None:
    """把行程顺带算出的热度写进 heat_snapshots（source='trip'）。

    为什么值得做：行程流程本来就要为**景点与美食**都算热度（all_items_for_heat），
    但此前只放在任务结果里，热度榜页读不到——用户想看榜还得为该城再刷一遍。
    落库后一次采集两处可用。

    与刷榜任务的差别只在"原料"：刷榜用 time_windows 拆近7天 / 7~60天 / 60天以上
    三窗口并据此判四态趋势；行程这边同样拿得到原始 VideoItem，所以这里补算一次，
    让两种来源的行结构完全一致——否则同一张表里会出现两种口径。

    趋势统一用 trend_of（与刷榜同口径），而不是 heat_index 的"近期热度上升/平稳"，
    避免一张榜单上并存两套趋势词汇。
    """
    from pipeline.heat import time_windows, trend_of

    for row in heat_rows:
        items = items_by_name.get(row["spot"]) or []
        w = time_windows(items) if items else {}
        knowledge.upsert_heat_snapshot(
            city, row["spot"],
            {
                "score": row["score"],
                "trend": trend_of(w.get("fresh7", 0.0), w.get("old60", 0.0), row["score"]),
                "fresh7": w.get("fresh7"), "fresh60": w.get("fresh60"),
                "old60": w.get("old60"),
                "likes": row.get("likes", 0), "videos": row.get("videos", 0),
                "mkt_ratio": row.get("mkt_ratio", 0), "sentiment": row.get("sentiment", ""),
            },
            kind="美食" if row["spot"] in food_names else "景点",
            source="trip",
        )


def _try_crawl(name: str, job_id: str | None, log, limit: int = TRIP_SPOT_LIMIT,
               queries: list[str] | None = None) -> list:
    """现场采集的安全阀：UGC 源未启用（kernel-only）时不抛错，返回空并提示走缓存/LLM 基线。

    limit/queries：采集条数与自定义搜索查询矩阵——城市攻略层用攻略词矩阵，
    逐点验证用默认（景点名 + 攻略 + 避雷 三角度）。"""
    global _UGC_WARNED
    try:
        return _crawl(name, limit, TRIP_SPOT_COMMENTS, False, job_id, log, queries=queries)
    except SourceDisabled:
        if not _UGC_WARNED:
            _UGC_WARNED = True
            log("未启用 UGC 数据源（SOURCE_DOUYIN_ENABLED=false）：跳过现场采集，仅用缓存/LLM 基线"
                "（kernel-only）；启用方法与责任见 README「合规与免责」")
        return []


# 天数中文表达（拼"三天两夜"这类抖音上真实存在的高信息密度搜索词）
_CN_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七"}


def _city_guide_queries(city: str, days: int) -> list[str]:
    """城市攻略层的搜索查询矩阵（纯函数，独立可测）。

    用户要去某地旅游，抖音上真正有信息密度的是"{城市}旅游攻略""{城市}三天两夜"
    这类综合攻略视频（含串线/住宿/取舍/避雷），而不是逐个景点名搜出来的打卡短视频。"""
    c = str(city or "").strip()
    d = max(1, int(days or 1))
    nights = max(1, d - 1)
    cn_d = _CN_NUM.get(d, str(d))
    cn_n = _CN_NUM.get(nights, str(nights))
    return [f"{c}旅游攻略", f"{c}{cn_d}天{cn_n}夜", f"{c}旅游避雷", f"{c}自由行攻略"]


def _city_guide_layer(city: str, days: int, job_id: str | None, log) -> dict:
    """第 0 阶段：城市攻略层。返回 extract_guide_knowledge 结构（失败给空骨架）。

    缓存优先：同城保鲜期内采过就直接复用已提炼结果（免采集、也免重复调 LLM）。
    本层是增强项不是必需项：UGC 源未启用、采集失败、提炼为空都返回空骨架，
    调用方自动降级为纯 LLM 圈定——绝不因它失败而阻断行程生成。"""
    if not CITY_GUIDE_ENABLED:
        return empty_guide_knowledge()
    try:
        cached = knowledge.find_guide(city, CITY_GUIDE_TTL_DAYS)
    except Exception as e:
        cached = None
        log(f"攻略层缓存查询失败（{e}），本轮现采")
    if cached and cached.get("guide"):
        g = cached["guide"]
        log(f"城市攻略层：缓存命中（{cached.get('crawled_at')}，{cached.get('video_count')} 条高赞攻略）——"
            f"实证候选 {len(g.get('guide_candidates') or [])} 个 / 编排建议 {len(g.get('plan_hints') or [])} 条")
        return g
    if not douyin_enabled():
        log("城市攻略层跳过：UGC 源未启用（kernel-only），圈定与排线走 LLM 基线")
        return empty_guide_knowledge()
    queries = _city_guide_queries(city, days)
    log(f"城市攻略层：采集高赞综合攻略（{'／'.join(queries)}）")
    try:
        items = _try_crawl(f"{city}旅游攻略", job_id, log,
                           limit=CITY_GUIDE_VIDEOS, queries=queries)
    except Cancelled:
        raise
    except Exception as e:
        log(f"城市攻略层采集失败（{e}），降级为纯 LLM 圈定")
        return empty_guide_knowledge()
    if not items:
        log("城市攻略层未采到内容（可能触发风控、未登录或该城攻略视频稀少），降级为纯 LLM 圈定")
        return empty_guide_knowledge()
    guide = extract_guide_knowledge(items, city=city, days=days)
    gc = guide.get("guide_candidates") or []
    hints = guide.get("plan_hints") or []
    if not gc and not hints:
        log("城市攻略层提炼为空（素材与攻略无关或 LLM 失败），降级为纯 LLM 圈定")
        return guide
    log(f"城市攻略层：{len(items)} 条高赞攻略 → 实证候选 {len(gc)} 个"
        f"（{'、'.join(m['name'] for m in gc[:8])}{'…' if len(gc) > 8 else ''}）"
        f"｜编排建议 {len(hints)} 条"
        + (f"｜建议天数 {guide['days_advice']}" if guide.get("days_advice") else "")
        + (f"｜住宿片区 {guide['stay_advice']}" if guide.get("stay_advice") else ""))
    # 落盘 + 登记：同城下次规划直接吃缓存（编排知识变化慢，TTL 比景点长）
    try:
        raw_path = save_raw(f"{city}城市攻略", items)
        knowledge.record_guide(city, raw_path, len(items), guide)
        log(f"城市攻略层已入库：{Path(raw_path).name}（保鲜 {CITY_GUIDE_TTL_DAYS} 天）")
    except Exception as e:
        log(f"城市攻略层入库失败（不影响本次结果）：{e}")
    return guide


def _guide_note(guide: dict, draft: dict | None = None) -> str:
    """攻略层的来源说明（进报告概览，让用户知道圈定与排线的实证依据从哪来）。

    无攻略层产出时返回空串（渲染层不输出该行，零回归）。纯函数，独立可测。"""
    g = guide or {}
    gc = g.get("guide_candidates") or []
    hints = g.get("plan_hints") or []
    if not gc and not hints:
        return ""
    n = g.get("videos") or 0
    bits = [f"城市攻略层 {n} 条高赞综合攻略视频" if n else "城市攻略层"]
    if gc:
        top = "、".join(m["name"] for m in gc[:6])
        bits.append(f"实证候选 {len(gc)} 个（{top}{'…' if len(gc) > 6 else ''}）")
    if hints:
        bits.append(f"编排建议 {len(hints)} 条")
    n_its = len(g.get("guide_itineraries") or [])
    if n_its:
        n_pts = len(draft_spot_names(draft)) if draft else 0
        bits.append(f"视频行程草案 {n_its} 条"
                    + (f"（审核后 {len(draft['days'])} 天 / {n_pts} 个点位，作为排线主干）" if n_pts else ""))
    if g.get("stay_advice"):
        bits.append(f"推荐住宿片区 {g['stay_advice']}")
    return "；".join(bits)


def start_trip(city: str, days: int, hotel: str, spots: list[str] | None,
               preferences: str = "",
               preference_mode: str = "均衡", start_date: str | None = None) -> str:
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
        args=(job_id, city, days, hotel, spots, preferences, preference_mode, start_date),
        daemon=True,
    ).start()
    return job_id


def _run_trip(job_id: str, city: str, days: int, hotel: str,
              user_spots: list[str] | None, preferences: str,
              preference_mode: str = "均衡",
              start_date: str | None = None) -> None:
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
        # 0) 城市攻略层：先搜"{城市}旅游攻略／N天N夜／避雷"这类综合攻略视频，
        #    从中提炼"真实被反复提到的点"与编排知识（串线/住宿片区/可跳过项）。
        #    从前圈定完全靠 LLM 凭空想象、排线只靠模型常识，这是报告"泛泛而谈"的根源。
        #    本层是增强项：未启用 UGC 源、采集失败或提炼为空都自动降级为纯 LLM 圈定。
        job["stage"] = "城市攻略层"
        guide = _city_guide_layer(city, days, job_id, log)
        guide_hints = [str(h).strip() for h in (guide.get("plan_hints") or []) if str(h).strip()]
        if guide.get("stay_advice") and not hotel:
            # 用户没给住宿时，把攻略推荐的片区作为排线参考（不伪造酒店名，只写进提示）
            guide_hints.append(f"未指定住宿：攻略推荐住在{guide['stay_advice']}片区，按此就近排线")
        if _cancelled():
            raise Cancelled()

        # 0.5) 视频行程草案：先看高赞攻略视频“实际是怎么排的”（逐日编排），
        #      由 LLM 审核合并/对齐用户天数/判断增删改，拿到草案后再把点位逐个丢去验证采集，
        #      最终规划以草案为主干——用户踩过的坑不重踩（无草案素材时自动跳过，零回归）。
        job["stage"] = "审核行程草案"
        draft_plan: dict = {}
        if guide.get("guide_itineraries"):
            draft_plan = review_guide_itinerary(city, days, preferences, guide)
            dn = draft_spot_names(draft_plan)
            if dn:
                log(f"视频行程草案：{len(guide['guide_itineraries'])} 条视频编排 → 审核后 "
                    f"{len(draft_plan['days'])} 天 / {len(dn)} 个点位"
                    f"（{'、'.join(dn[:8])}{'…' if len(dn) > 8 else ''}）")
                if draft_plan.get("notes"):
                    log(f"  审核说明：{draft_plan['notes']}")
            else:
                draft_plan = {}
                log("视频行程草案审核未产出有效编排，本次不注草案（零回归）")
        draft_names = draft_spot_names(draft_plan)
        if _cancelled():
            raise Cancelled()

        # 1) 圈定候选：用户指定 > 混合候选验证（未指定清单时）> 简单圈定兜底
        job["stage"] = "圈定景点"
        categories: dict[str, str] = {}
        cands: list[dict] = []           # 全量候选（含被截断未验证的），供选点决策表展示（F1.1/F2.1）
        verify_results: list[dict] = []  # 交叉验证结论（含 drop），供决策表与门禁 R2
        pre: dict[str, tuple[list, list[dict]]] | None = None
        if user_spots:
            spots = [s.strip() for s in user_spots if s.strip()][:days * MAX_SPOTS_PER_DAY]
            log(f"使用用户指定景点清单：{'、'.join(spots)}")
            food_names: list[str] = []
            try:
                food_names = candidate_foods(city, max(TRIP_FOOD_LIMIT, FOOD_RANK_MIN_SAMPLES))
                if food_names:
                    log(f"美食候选：{'、'.join(food_names)}（将单独调研供美食推荐榜）")
            except Exception as e:
                log(f"美食候选圈定失败（{e}），美食榜将缺样本")
            food_pre: dict[str, tuple[list, list[dict]]] = {}
        else:
            spots = []
            try:
                cands = generate_candidates(city, days, preferences, guide_evidence=guide,
                                            draft_plan=draft_plan or None)
                log(f"圈定 {len(cands)} 个候选"
                    + (f"（以 {len(guide.get('guide_candidates') or [])} 个攻略实证点为底稿）"
                       if guide.get("guide_candidates") else "（无攻略层实证，纯 LLM 基线）")
                    + (f"，草案 {len(draft_names)} 个点位优先验证" if draft_names else "")
                    + f"；按类别配额公平挑选验证（上限 {VERIFY_MAX} 个）")
                verify_cands = select_verify_candidates(cands, VERIFY_MAX, priority=draft_names)
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
                        items = _try_crawl(name, job_id, log)
                        if items:
                            raw_path = save_raw(name, items)
                            knowledge.record_crawl(
                                name, raw_path, len(items), sum(len(x.comments) for x in items)
                            )
                    # 验证统计：营销号占比（文案正则）+ 预提取立场计数 + 评论摘录样本
                    mkt = sum(1 for it in items if is_marketing(it.description)) if items else 0
                    pts: list[dict] = []
                    extract_fails = 0
                    if items:
                        failed_items: list = []
                        with ThreadPoolExecutor(max_workers=3) as pool:
                            fut_map = {pool.submit(extract_points, it): it for it in items}
                            for fut in as_completed(fut_map):
                                try:
                                    pts.extend(fut.result())
                                except Exception:
                                    failed_items.append(fut_map[fut])
                        # 偶发 LLM 失败重试一次：避免"有视频却因提取失败被当无证据淘汰"（故宫式误杀）
                        for it in failed_items:
                            try:
                                pts.extend(extract_points(it))
                            except Exception:
                                extract_fails += 1
                        if extract_fails:
                            log(f"    {name}：{extract_fails}/{len(items)} 条要点提取失败（已重试仍失败）")
                    pos = sum(1 for p in pts if p.get("stance") == "推荐")
                    neg = sum(1 for p in pts if p.get("stance") == "避雷")
                    # 置信度标注：同景点组内交叉验证 + 营销号来源降级（后续档案/避坑复用同一批要点）
                    if pts:
                        mkt_src = {it.url for it in items if is_marketing(it.description)}
                        pts = annotate_confidence(pts, marketing_sources=mkt_src)
                    vstats[name] = {
                        "videos": len(items), "marketing_hits": mkt,
                        "positive": pos, "negative": neg,
                        "sample_quotes": [p.get("quote") for p in pts if p.get("quote")][:3],
                    }
                    researched[name] = (items, pts)
                    categories[name] = cand["category"]
                results = verify_candidates(verify_cands, vstats)
                verify_results = results
                kept = [r["name"] for r in results if r["verdict"] == "keep" and r["name"] in researched]
                dropped = [r["name"] for r in results if r["verdict"] == "drop"]
                if dropped:
                    log(f"交叉验证淘汰 {len(dropped)} 个：{'、'.join(dropped[:6])}")
                # 景点优先（行程骨架）；美食候选分流去餐厅调研线（不进景点排序）
                kept.sort(key=lambda n: 0 if categories.get(n) == "景点" else 1)
                pre = {n: researched[n] for n in kept if categories.get(n) != "美食"}
                food_names = [n for n in kept if categories.get(n) == "美食"][:max(TRIP_FOOD_LIMIT, FOOD_RANK_MIN_SAMPLES)]
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
                    items = _try_crawl(name, job_id, log)
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
                # 置信度标注：景点/餐厅组内交叉验证，营销号来源降为低置信度
                mkt_src = {it.url for it in items if is_marketing(it.description)}
                pts = annotate_confidence(pts, marketing_sources=mkt_src)
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
            log(f"餐厅调研完成：{len(food_points)}/{len(food_names)} 家，供美食推荐榜（不排入时间线）")
        else:
            log("无可用餐厅调研结果，美食榜将缺样本（不编造餐厅）")
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

        # 4) 交通方案：高德 Key 在时给具体线路（公交/地铁站数+票价+打车费用）；
        #    无 Key 降级为 LLM 交通估算（标注"以地图App为准"），两者都失败才纯区域排线
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

            def _leg(a: str, b: str) -> None:
                adv = geo.route_advice(locs[a], locs[b], city)
                if adv:
                    travel_lines.append(f"{a}->{b}: {adv}")

            if "酒店" in locs:
                for s in spot_points:
                    if s in locs:
                        _leg("酒店", s)
            names = [s for s in spot_points if s in locs]
            for a in names:
                for b in names:
                    if a != b:
                        _leg(a, b)
            log(f"高德交通方案：{len(travel_lines)} 条路段（含公交线路/站数/票价/打车估算）")
            if not travel_lines:
                log("高德路线查询均失败，回退 LLM 交通估算")
        if not travel_lines:
            travel_lines = transport_hints(city, hotel, list(spot_points.keys()))
            if travel_lines:
                log(f"LLM 交通估算：{len(travel_lines)} 条路段（均标注'估算，以地图App为准'）")
            else:
                log("LLM 交通估算也失败：降级为纯区域排线，transport 由规划模型给大致方案")
        if _cancelled():
            raise Cancelled()

        # 4.5) 主体归属校验：剔除挂错地点、实为描述别处的要点（串档），宁可漏剔不误删
        misattr = apply_attribution_check(profiles)
        if misattr:
            n_rm = sum(len(v) for v in misattr.values())
            log(f"归属校验：剔除 {n_rm} 条疑似描述别处的条目（涉及 {'、'.join(list(misattr)[:4])}）")

        # 与 plan 无关的先算：避坑专题 + 热度榜
        all_points = [p for pts in spot_points.values() for p in pts] \
            + [p for pts in food_points.values() for p in pts]
        pitfall = pitfall_digest(all_points)
        heat_rows: list[dict] = []
        for s, items in all_items_for_heat.items():
            if items:
                h = heat_index(items)
                h["spot"] = s
                # 评论情感趋势：行程调研采了评论，顺手算近30天好评率走向（纯函数零成本）
                h["sentiment"] = sentiment_trend([c for it in items for c in it.comments])["trend"]
                heat_rows.append(h)
        heat_rows.sort(key=lambda r: r["score"], reverse=True)
        # 热度+证据摘要喂规划：优先证据强、热度高的点
        heat_by_name = {h["spot"]: h for h in heat_rows}
        hs_lines: list[str] = []
        for name in list(profiles.keys())[:12]:
            ev = spot_evidence(None, spot_points.get(name, []))
            h = heat_by_name.get(name)
            hs_lines.append(
                f"{name}：证据{ev}｜热度{h['score']:.2f} {h['trend']}" if h
                else f"{name}：证据{ev}")
        heat_summary = "\n".join(hs_lines)

        # 5) 规划生成（以视频行程草案为主干；餐饮解耦为美食榜，不排餐厅进时间线）
        job["stage"] = "生成规划"
        plan = plan_itinerary(city, days, hotel, profiles, travel_lines, preferences,
                              preference_mode, heat_summary=heat_summary,
                              guide_hints=guide_hints, draft_plan=draft_plan or None)
        if not plan["days"]:
            raise RuntimeError("规划生成失败：未产出有效行程，请重试或减少天数/景点")

        # 5.5) 统一决策对象 + 质量门禁 + 有限回炉：
        #      生成与裁判分离——门禁是独立确定性规则，不达标带 issues 回炉，仍不达标进已知妥协
        job["stage"] = "质量门禁"
        today = datetime.now().strftime("%Y-%m-%d")
        # 出发日→每日星期：有则 R3 运行期真校闭馆日，无则 [] 使 R3 退回不判闭馆
        day_weekdays = day_weekdays_from(start_date, days)
        if day_weekdays:
            log(f"出发日 {start_date}：行程各日星期 {'、'.join(day_weekdays)}（R3 将据官方闭馆日校验）")

        # 官方事实层：命中种子/高德的点喂决策与门票同源展示；无则留空标"待核实"
        _of_names = list(profiles) + [str(c.get("name") or "").strip()
                                      for c in (cands or []) if isinstance(c, dict)]
        official_map = load_city_facts(city, _of_names, with_amap=geo.available())
        if official_map:
            log(f"官方事实层：命中 {len(official_map)} 点（种子/高德），行程门票以官方/同源价展示")

        def _build_decs(p: dict) -> list:
            return build_decisions(
                candidates=cands, verify_results=verify_results,
                profiles=profiles, food_profiles=food_profiles,
                points_by_spot={**spot_points, **food_points},
                sources_by_spot={**spot_sources, **food_sources},
                heat_rows=heat_rows, locs=locs, travel_lines=travel_lines,
                official_facts=official_map, plan=p)

        def _gate(p: dict, decs: list):
            # 时间可行性（R4）与路线闸（R5）随 plan 变：用当版 plan 现算 Leg（纯函数零成本）
            lg = build_legs(city, hotel, p, locs, travel_lines)
            return run_quality_gate(decisions=decs, plan=p, profiles=profiles, days=days,
                                    pitfall=pitfall,
                                    food_profiles=food_profiles, locs=locs, today=today,
                                    day_weekdays=day_weekdays, legs=lg)

        decisions = _build_decs(plan)
        report = _gate(plan, decisions)
        rounds = 0
        while report.fails and rounds < QC_REPAIR_ROUNDS:
            if _cancelled():
                raise Cancelled()
            rounds += 1
            fails = "、".join(c.rule_id for c in report.fails)
            log(f"质量门禁 {report.score} 分，{len(report.fails)} 项不达标（{fails}），回炉重排第 {rounds} 轮")
            plan2 = plan_itinerary(city, days, hotel, profiles, travel_lines, preferences,
                                   preference_mode, extra_issues=report.issues,
                                   heat_summary=heat_summary,
                                   guide_hints=guide_hints, draft_plan=draft_plan or None)
            if not plan2["days"]:
                break
            decs2 = _build_decs(plan2)
            report2 = _gate(plan2, decs2)
            if report2.problem_count() < report.problem_count():   # 只采纳问题数严格变少的版本
                plan, decisions, report = plan2, decs2, report2
                log(f"回炉第 {rounds} 轮采纳：质量分升至 {report.score}，问题数降至 {report.problem_count()}")
            else:
                log(f"回炉第 {rounds} 轮未变好（问题数 {report2.problem_count()} ≥ {report.problem_count()}），保留上一版")
                break
        finalize(report, repair_rounds=rounds)

        # 定稿 Leg 汇总：交通方案随最终 plan 重算，供报告"分段交通"与 R4/R5 复用
        legs = build_legs(city, hotel, plan, locs, travel_lines)
        log(f"行程定稿：{len(plan.get('days') or [])} 天；避坑 {len(pitfall)} 条；"
            f"热度榜 {len(heat_rows)} 个；质量分 {report.score}"
            + (f"；已知妥协 {len(report.unresolved)} 项" if report.unresolved else "")
            + (f"；排布兜底搬移 {len(plan.get('moved') or [])} 处" if plan.get("moved") else ""))
        if _cancelled():
            raise Cancelled()

        # 顺带把这次采集到的热度落库（source=trip）：本次已为景点+美食都算了热度，
        # 落库后用户去热度榜不必再为该城单独刷一遍（一次采集，两处可用）。
        # 写库失败不影响行程本身——榜单是顺带的产物，不该因为它毁掉整单。
        try:
            _persist_heat(city, heat_rows, all_items_for_heat, set(food_profiles.keys()))
            log(f"热度快照已落库：{len(heat_rows)} 条（热度榜可直接查看，无需再刷榜）")
        except Exception as e:
            log(f"热度快照落库失败（不影响行程与路书）：{e}")

        # 6) 渲染落盘：Markdown + HTML 可视化双输出（共享同一时间戳文件名）
        job["stage"] = "渲染路书"
        ts = datetime.now()
        # 决策层产出唯一 TripPlan，并据它补判 R11/R12（输出完整性，不入回炉）
        # 呈现快照由编排层用决策层纯函数预先整理（表达层只读，零计算）
        snap = {
            "user_spots": user_spots,
            "overview": build_overview(days, plan, profiles, pitfall, food_profiles or None),
            "pitfall": pitfall, "digests": digests, "legs": legs,
            "geo_on": geo.available(),
            "summary_note": plan.get("summary_note", ""),
            # 攻略层的实证来源必须对用户可见（圈定与排线的依据到底从哪来）
            "guide_note": _guide_note(guide, draft_plan),
            # 重复点位剔除必须对用户可见（不静默改行程）
            "dedupe_note": (("已剔除重复排入的点位：" + "、".join(plan.get("duplicate_drops") or [])
                             + "（每个景点全程只排一次）")
                            if plan.get("duplicate_drops") else ""),
            # 排布兜底搬移也必须对用户可见（不静默改行程）
            "rebalance_note": (("已均衡排布：" + "；".join(plan.get("moved") or [])
                                + "（避免某些天排得太空/太满）")
                               if plan.get("moved") else ""),
            "profiles": profiles, "foods": food_profiles or {}, "heat": heat_rows,
        }
        trip_plan = build_trip_plan(
            meta={"city": city, "days": days, "stay": hotel,
                  "prefs": preferences, "preference_mode": preference_mode,
                  "start_date": start_date,
                  "collect_mode": "douyin+cache" if douyin_enabled() else "kernel-only",
                  "guide_videos": guide.get("videos") or 0,   # 攻略层实证视频数（0=未启用/已降级）
                  "draft_days": len(draft_plan.get("days") or []),  # 视频行程草案天数（0=无草案）
                  "generated_at": ts.isoformat(timespec="seconds"),
                  "facts_cutoff": today, "synthetic": False},
            decisions=decisions, plan=plan,
            quality=report, legs=legs, snap=snap,
            food_min_samples=FOOD_RANK_MIN_SAMPLES,
            guide=guide)      # 攻略层实证补进 catalog（美食榜排序与详情展示用）
        apply_trip_plan_checks(report, trip_plan.to_dict())
        trip_plan.quality = report.to_dict()          # 刷新内嵌质量（含 R11/R12 真判）
        tp = trip_plan.to_dict()
        md = render_tp_md(tp)   # 只读渲染：唯一 TripPlan → 0~11 报告 IA（旧 render_trip 已删，双写收敛）
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
            html_path.write_text(render_tp_html(tp), encoding="utf-8")
            log(f"行程已保存：{report_path.name}（含 HTML 可视化版 {html_path.name}）")
        except Exception as e:
            html_path = None
            log(f"HTML 渲染失败（不影响 Markdown 结果）：{e}")
        # 成本可见：如实报本次 LLM 消耗
        log(usage_note() + "（用量可在服务商控制台核对，本项目不发联网搜索请求）")

        job["result"] = {
            "report_path": str(report_path),
            "report_name": report_path.name,
            "html_name": html_path.name if html_path else None,
            "markdown": md,
            "cache_hit": False,
            "draft_plan": draft_plan,   # 视频行程草案（规划主干，API/前端可直接展示）
            "pitfall_digest": pitfall,
            "heat_rank": heat_rows,
            "quality": report.to_dict(),
            "decision_table": [d.to_row() for d in decisions],
            "trip_plan": trip_plan.to_dict(),   # 唯一顶层对象，API/MCP 直接可取
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
