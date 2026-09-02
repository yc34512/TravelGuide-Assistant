"""行程规划层：候选圈定 + 景点档案蒸馏 + 行程生成 + 路书渲染（Markdown + HTML）。

与攻略管道的分工：本层零采集，输入全部来自知识库已有的要点，
输出是按时间/空间维度排布的行程路书。所有 LLM 输出都做防御性规范化，
结构不合法宁可降级也不把脏数据传下去。
"""
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import LLM_WEB_SEARCH
from core.llm import chat_json

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

ALLOWED_SLOTS = {"上午", "下午", "晚上"}

CANDIDATE_SYSTEM = """你是旅行规划师。根据城市与出行天数，列出该城市最值得去的景点名单。
规则：
1. 数量不超过给定上限，优先选标志性、口碑好、适合大众游客的景点；
2. 只输出景点名（不带城市名前缀、不重复、不含餐厅/酒店）；
3. 输出严格 JSON：{"spots": ["..."]}"""

PROFILE_SYSTEM = """你是旅游数据分析师。给定某景点的编号信息要点（含置信度与立场标注），
蒸馏出一份用于行程规划与预算控制的结构化档案。

规则：
1. 只使用给定要点中的信息，禁止用自己的知识补充；某字段没有依据就给空值；
2. duration_hours：预估游玩时长（小时，可为 2.5 这类小数）；要点无依据时给 null；
3. best_time_slot：上午/下午/晚上/全天 四选一，依据要点中的时段建议；无依据给"全天"；
4. avoid 收录"避雷"立场的要点；时效敏感与其他注意事项归入 tips；
5. cost_items：从要点提取的确定花费，每条 {"item": "名称", "type": "门票|餐饮人均|交通|其他", "amount": 数字}；
   只收录要点中有明确数字的花费，估算与无依据的一律不写；无则空数组；
6. 每条文本一句话，保留具体事实（数字、地名、时间），不要空泛概括；
7. 输出严格 JSON：{"duration_hours": 数字或null, "best_time_slot": "上午|下午|晚上|全天",
   "highlights": ["..."], "avoid": ["..."], "food": ["..."], "photo_spots": ["..."], "tips": ["..."],
   "cost_items": [{"item": "...", "type": "门票|餐饮人均|交通|其他", "amount": 数字}]}"""

PLAN_SYSTEM = """你是专业行程规划师。基于景点档案与通行时间数据，生成逐日分时段的行程规划。

规则：
1. 每天分上午/下午/晚上三个时段，每时段安排 0~2 个景点（行程要留白，不要排满）；
2. 顺路优先：同一天安排通行时间数据中相距近的景点（同区域聚类，避免来回折返）；每天从酒店出发，晚上回酒店附近；
3. 尊重景点的 best_time_slot，尽量把景点排在它最佳的时段；
4. 景点档案中的 avoid 与 tips 必须完整写进该景点的 notes，不得省略；
5. transport 必须具体：给出了交通方案数据的路段，直接引用其中的线路/时长/费用（保留"约""估算"字样）；
   未给出的路段按距离给出大致方案（如"打车约X元/约X分钟，具体以地图App为准"），禁止只写"建议查地图"；
6. 每天的午餐与晚餐各从给定餐厅候选中选一家推荐，写进当天对应时段的 food 字段
   （午餐→下午时段，晚餐→晚上时段），格式："店名：推荐菜（人均X元）"；
   同一家餐厅全程最多推荐一次；没给餐厅候选或当天时段没排点位时写空字符串；
7. spot 只能从给定景点名单中选择；每个景点全程只出现一次；
8. reasons 一句话说明为什么值得去（来自档案 highlights）；
9. cost：该点位预估花费（元，数字）：门票类花费按档案 cost_items 计入；免费或未知写 0；
10. pitfall_quotes：从该景点档案 avoid 对应的要点中挑最重要的 1~2 条评论原文引用（逐字，不改写）；没有则空数组；
11. 预算约束：若给出了总预算，全部点位 cost 合计加上餐饮/交通估算不得显著超出；
    超支时优先去掉"体验/购物"类点位并在 summary_note 说明；省钱优先模式下优先免费/低价点位；
12. summary_note：一句话说明预算匹配情况（结余/超支及调整建议），无预算时为空字符串；
13. 输出严格 JSON：{"summary_note": "...", "days": [{"day": 1, "slots": [{"slot": "上午|下午|晚上", "spot": "景点名",
   "duration": "约X小时", "transport": "从上一地点至此的方式与耗时", "cost": 数字,
   "reasons": "...", "notes": "...", "food": "", "pitfall_quotes": ["评论原文"]}]}]}"""

TRANSPORT_SYSTEM = """你是本地交通向导。给定城市、住宿位置与景点清单，为各段路线给出具体交通建议。
规则：
1. 覆盖：住宿到每个景点各一条；景点之间挑地理位置邻近或常被同天游览的组合给出（总数不超过给定上限）；
2. 每条建议包含：公交/地铁（线路名+约几分钟+约几元；不确定具体线路时写"公交/地铁约X分钟，线路以地图App为准"）
   与打车（约X元/约X分钟，按当地里程估算）；1.5公里内的写步行约X分钟；
3. 所有数字前必须带"约"，是估算不是实测；禁止编造精确票价；
4. 输出严格 JSON：{"routes": [{"from": "起点名", "to": "终点名", "advice": "..."}]}"""

DIGEST_SYSTEM = """你是评价分析专家。给定某景点的编号信息要点（含立场标注与部分评论原文引用），
压缩成一份"真实评价摘要"，供游客出发前一分钟读完。

规则：
1. 只用给定要点中的信息，禁止编造；某字段无对应内容就给空字符串/空数组；
2. verdict：一句话总评（不超过 25 字），客观概括口碑情况（如"口碑两极：景观震撼但体力劝退"）；
3. positive：好评摘要（不超过 60 字），压缩"推荐"立场的要点，保留具体事实；
4. negative：差评摘要（不超过 60 字），压缩"避雷"立场的要点，保留具体事实；
5. quotes：从要点自带的评论原文引用中挑最有代表性的 2 条（逐字照抄不改写）；
6. 输出严格 JSON：{"verdict": "...", "positive": "...", "negative": "...", "quotes": ["..."]}"""


def candidate_spots(city: str, days: int, max_n: int) -> list[str]:
    """让 LLM 圈定城市候选景点（去重、限量）。"""
    data = chat_json(
        CANDIDATE_SYSTEM,
        f"城市：{city}\n出行天数：{days}\n景点数量上限：{max_n}",
    )
    seen: set[str] = set()
    out: list[str] = []
    for x in data.get("spots") or []:
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:max_n]


def _normalize_profile(data: dict) -> dict:
    """档案防御性规范化：字段补齐、非法值降级。纯函数，独立可测。"""
    def lst(k: str) -> list[str]:
        v = data.get(k)
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    dur = data.get("duration_hours")
    try:
        dur = float(dur) if dur is not None else None
        if dur is not None and not (0.5 <= dur <= 24):
            dur = None
    except (TypeError, ValueError):
        dur = None
    slot = str(data.get("best_time_slot") or "").strip()
    # 花费项：只保留名称与金额都合法且金额>0 的条目（预算计算的原料，脏数据一律丢弃）
    cost_items = []
    for x in data.get("cost_items") or []:
        if not isinstance(x, dict):
            continue
        item = str(x.get("item") or "").strip()
        ctype = str(x.get("type") or "").strip()
        try:
            amount = float(x.get("amount"))
        except (TypeError, ValueError):
            continue
        if item and amount > 0:
            cost_items.append(
                {"item": item, "type": ctype if ctype in ("门票", "餐饮人均", "交通", "其他") else "其他",
                 "amount": round(amount, 2)}
            )
    return {
        "duration_hours": dur,
        "best_time_slot": slot if slot in ALLOWED_SLOTS | {"全天"} else "全天",
        "highlights": lst("highlights"),
        "avoid": lst("avoid"),
        "food": lst("food"),
        "photo_spots": lst("photo_spots"),
        "tips": lst("tips"),
        "cost_items": cost_items,
    }


def empty_profile() -> dict:
    return {
        "duration_hours": None, "best_time_slot": "全天",
        "highlights": [], "avoid": [], "food": [], "photo_spots": [], "tips": [],
        "cost_items": [],
    }


def build_spot_profile(spot: str, points: list[dict]) -> dict:
    """从某景点的全部要点蒸馏结构化档案（单次 LLM 调用）。"""
    numbered = "\n".join(
        f"[{i + 1}] ({p.get('topic', '其他')}|{p.get('confidence', '单源')}|{p.get('stance', '中性')}) {p['claim']}"
        for i, p in enumerate(points)
    )
    data = chat_json(PROFILE_SYSTEM, f"景点：{spot}\n信息要点：\n{numbered or '(无)'}")
    return _normalize_profile(data)


def empty_digest() -> dict:
    return {"verdict": "", "positive": "", "negative": "", "quotes": []}


def _normalize_digest(data: dict, quote_pool: list[str]) -> dict:
    """摘要防御性规范化：引文防幻觉校验（必须真实出自输入要点自带的原文）。纯函数可测。"""
    raw_quotes = [str(q).strip()[:80] for q in (data.get("quotes") or []) if str(q).strip()]
    quotes = [
        q for q in raw_quotes
        if any(q[:15] in p or p[:15] in q for p in quote_pool)
    ][:2]
    return {
        "verdict": str(data.get("verdict") or "").strip()[:60],
        "positive": str(data.get("positive") or "").strip()[:120],
        "negative": str(data.get("negative") or "").strip()[:120],
        "quotes": quotes,
    }


def build_review_digest(spot: str, points: list[dict]) -> dict:
    """把景点的推荐/避雷/中性要点压缩成真实评价摘要（单次 LLM 调用）。
    返回 {"verdict", "positive", "negative", "quotes"}；调用方失败时降级为空摘要。"""
    quote_pool = [p["quote"] for p in points if p.get("quote")]
    numbered = "\n".join(
        f"[{i + 1}] ({p.get('stance', '中性')}) {p['claim']}"
        + (f"（评论原文：{p['quote']}）" if p.get("quote") else "")
        for i, p in enumerate(points)
    )
    data = chat_json(DIGEST_SYSTEM, f"景点：{spot}\n信息要点：\n{numbered or '(无)'}")
    return _normalize_digest(data, quote_pool)


def _normalize_plan(data: dict, allowed_spots: set[str], days: int) -> dict:
    """行程防御性规范化：丢弃不在名单中的景点、修正非法时段、限制天数。纯函数可测。"""
    out_days = []
    for d in (data.get("days") or [])[:days]:
        slots = []
        for s in d.get("slots") or []:
            spot = str(s.get("spot") or "").strip()
            slot = str(s.get("slot") or "").strip()
            if not spot or spot not in allowed_spots:
                continue
            # cost：数字或"约120元"这类文本都尽量解析成数字，失败记 0（预算不中断）
            raw_cost = s.get("cost")
            try:
                cost = float(str(raw_cost).replace("，", "").replace("元", "").strip() or 0)
            except ValueError:
                import re as _re
                m = _re.search(r"[\d.]+", str(raw_cost) or "")
                cost = float(m.group()) if m else 0.0
            quotes = s.get("pitfall_quotes")
            quotes = [str(q).strip()[:80] for q in quotes if str(q).strip()] if isinstance(quotes, list) else []
            slots.append(
                {
                    "slot": slot if slot in ALLOWED_SLOTS else "下午",
                    "spot": spot,
                    "duration": str(s.get("duration") or "").strip(),
                    "transport": str(s.get("transport") or "").strip(),
                    "cost": round(cost, 2),
                    "reasons": str(s.get("reasons") or "").strip(),
                    "notes": str(s.get("notes") or "").strip(),
                    "food": str(s.get("food") or "").strip(),
                    "pitfall_quotes": quotes,
                }
            )
        if slots:
            out_days.append({"day": len(out_days) + 1, "slots": slots})
    return {"days": out_days, "summary_note": str(data.get("summary_note") or "").strip()}


def transport_hints(city: str, hotel: str, spots: list[str], max_routes: int = 15) -> list[str]:
    """无高德 Key 时的降级：LLM 按城市常识生成具体交通估算（一次调用，联网能力随配置生效）。

    返回 ["起点->终点: 建议（估算）"] 供规划提示词引用；失败返回空列表（不阻断行程）。
    """
    names = [s for s in spots if s][:8]
    if not names:
        return []
    user = (f"城市：{city}\n住宿位置：{hotel or '未指定（按市中心算）'}\n"
            f"景点清单：{'、'.join(names)}\n路线条数上限：{max_routes}")
    try:
        data = chat_json(TRANSPORT_SYSTEM, user, web_search=LLM_WEB_SEARCH)
    except Exception:
        return []
    out: list[str] = []
    for r in data.get("routes") or []:
        if not isinstance(r, dict):
            continue
        a = str(r.get("from") or "").strip()
        b = str(r.get("to") or "").strip()
        adv = str(r.get("advice") or "").strip()
        if a and b and adv:
            out.append(f"{a}->{b}: {adv}（估算，以地图App为准）")
        if len(out) >= max_routes:
            break
    return out


def plan_itinerary(city: str, days: int, hotel: str, profiles: dict[str, dict],
                   travel_lines: list[str], preferences: str,
                   budget: float | None = None, preference_mode: str = "均衡",
                   foods: dict[str, dict] | None = None) -> dict:
    """一次 LLM 调用生成行程 JSON，随后做防御性规范化。传入预算时启用预算约束规则；
    传入 foods（餐厅档案）时启用每日午/晚餐推荐规则。"""
    profile_lines = []
    for name, p in profiles.items():
        dur = f"约{p['duration_hours']}小时" if p["duration_hours"] else "时长未知"
        costs = "；".join(f"{c['item']}({c['type']}){c['amount']}元" for c in p.get("cost_items", [])) or "无明确花费数据"
        profile_lines.append(
            f"【{name}】最佳时段:{p['best_time_slot']} | 建议时长:{dur}\n"
            f"  花费: {costs}\n"
            f"  亮点: {'; '.join(p['highlights']) or '无'}\n"
            f"  避雷: {'; '.join(p['avoid']) or '无'}\n"
            f"  美食: {'; '.join(p['food']) or '无'}\n"
            f"  注意: {'; '.join(p['tips']) or '无'}"
        )
    budget_line = (
        f"总预算：{budget:.0f} 元（不含大交通）｜消费偏好：{preference_mode}\n" if budget else "总预算：未指定\n"
    )
    user = (
        f"城市：{city}\n天数：{days}\n住宿酒店：{hotel or '未指定'}\n"
        + budget_line
        + f"用户偏好：{preferences or '无'}\n\n"
        f"景点档案：\n" + "\n".join(profile_lines) + "\n\n"
        + f"交通方案数据（实测或估算，transport 优先引用）：\n"
        + ("\n".join(travel_lines) if travel_lines else "（无，按距离给出大致方案并标注'以地图App为准'）")
    )
    if foods:
        food_lines = []
        for name, p in foods.items():
            costs = "；".join(f"{c['item']}{c['amount']:.0f}元" for c in p.get("cost_items", [])) or "人均未知"
            food_lines.append(
                f"【{name}】{costs}\n"
                f"  招牌/推荐: {'; '.join(p['highlights']) or '无'}\n"
                f"  避雷: {'; '.join(p['avoid']) or '无'}"
            )
        user += "\n\n餐厅候选（每日午餐/晚餐从中推荐，每家最多推荐一次）：\n" + "\n".join(food_lines)
    data = chat_json(PLAN_SYSTEM, user)
    return _normalize_plan(data, set(profiles.keys()), days)


def build_budget_summary(profiles: dict[str, dict], plan: dict, days: int,
                         budget: float | None) -> dict:
    """预算明细汇总：门票（多源去重后求和）/餐饮（人均×天数×2 正餐）/市内交通估算/弹性 10%。
    纯函数，独立可测；无预算时 total_budget 为 None。"""
    tickets = []
    food_prices = []
    for p in profiles.values():
        for c in p.get("cost_items", []):
            if c["type"] == "门票":
                tickets.append(round(c["amount"]))
            elif c["type"] == "餐饮人均":
                food_prices.append(c["amount"])
    ticket_total = float(sum(dict.fromkeys(tickets)))  # 同价位门票去重（多个来源说同一票价）
    avg_meal = round(sum(food_prices) / len(food_prices)) if food_prices else 50
    food_total = float(avg_meal * days * 2)
    slot_count = sum(len(d["slots"]) for d in plan.get("days", []))
    transport = float(max(slot_count, 1) * 15)  # 估算值：每点位市内交通约 15 元（无实测数据时的保守占位）
    subtotal = ticket_total + food_total + transport
    flex = round(subtotal * 0.1, 2)
    total = round(subtotal + flex, 2)
    out = {
        "tickets": ticket_total,
        "food": food_total,
        "food_avg": float(avg_meal),
        "transport": transport,
        "flex": flex,
        "total": total,
        "total_budget": float(budget) if budget else None,
        "status": "",
        "note": "",
    }
    if budget:
        if total <= budget:
            out["status"] = "结余"
            out["note"] = f"预估总花费 {total:.0f} 元，预算内结余约 {budget - total:.0f} 元"
        else:
            out["status"] = "超支"
            out["note"] = (f"预估总花费 {total:.0f} 元，超出预算约 {total - budget:.0f} 元；"
                           "建议：减少付费景点、选免费替代或降低餐饮标准")
    return out


def build_overview(days: int, plan: dict, profiles: dict[str, dict],
                   budget_summary: dict | None, pitfall: list[dict] | None,
                   foods: dict[str, dict] | None = None) -> dict:
    """行程概览卡数据：天数/景点与餐厅数/总预算与日均/亮点与避坑数量。纯函数可测。"""
    total_slots = sum(len(d.get("slots", [])) for d in plan.get("days", []))
    total = budget_summary["total"] if budget_summary else None
    return {
        "days": days,
        "spots": len(profiles),
        "foods": len(foods or {}),
        "slots": total_slots,
        "total_cost": total,
        "daily_cost": round(total / days, 2) if (total and days) else None,
        "highlights": sum(len(p.get("highlights", [])) for p in profiles.values()),
        "pitfalls": len(pitfall or []),
    }


def day_subtotals(plan: dict, budget_summary: dict | None) -> list[dict]:
    """每日花费小计：点位花费 + 餐饮/交通按天摊（弹性不分摊，留在总计里）。纯函数可测。"""
    n_days = len(plan.get("days", [])) or 1
    food_d = round(budget_summary["food"] / n_days) if budget_summary else 0
    trans_d = round(budget_summary["transport"] / n_days) if budget_summary else 0
    out = []
    for d in plan.get("days", []):
        spot_sum = round(sum(s.get("cost") or 0 for s in d.get("slots", [])))
        out.append({"day": d.get("day"), "spots": spot_sum, "food": food_d,
                    "transport": trans_d, "total": spot_sum + food_d + trans_d})
    return out


def budget_breakdown(profiles: dict[str, dict], budget_summary: dict | None,
                     days: int) -> list[dict]:
    """预算明细行：门票逐点列明 / 餐饮 / 交通 / 弹性。纯函数可测。"""
    if not budget_summary:
        return []
    ticket_items = []
    food_items = []
    for name, p in profiles.items():
        for c in p.get("cost_items", []):
            if c["type"] == "门票":
                ticket_items.append(f"{name} {c['item']} {c['amount']:.0f} 元")
            elif c["type"] == "餐饮人均":
                food_items.append(f"{name} 人均 {c['amount']:.0f} 元")
    food_detail = f"人均 {budget_summary['food_avg']:.0f} 元 × 2 正餐 × {days} 天"
    if food_items:
        food_detail += "（调研到的餐厅人均：" + "；".join(food_items) + "）"
    return [
        {"item": "门票", "detail": "；".join(ticket_items) or "无门票数据", "amount": budget_summary["tickets"]},
        {"item": "餐饮", "detail": food_detail, "amount": budget_summary["food"]},
        {"item": "市内交通", "detail": "估算（每行程点约 15 元）", "amount": budget_summary["transport"]},
        {"item": "弹性预留", "detail": "前几项小计的 10%", "amount": budget_summary["flex"]},
    ]


def render_trip(city: str, days: int, hotel: str, plan: dict, profiles: dict[str, dict],
                spot_sources: dict[str, list[str]], geo_on: bool,
                budget_summary: dict | None = None, pitfall: list[dict] | None = None,
                heat: list[dict] | None = None, digests: dict[str, dict] | None = None,
                foods: dict[str, dict] | None = None,
                food_sources: dict[str, list[str]] | None = None) -> str:
    """把行程 JSON 渲染成 Markdown 路书（逐日卡片 + 预算 + 避坑专题 + 热度榜 + 景点详情 + 来源链接）。"""
    total_slots = sum(len(d["slots"]) for d in plan["days"])
    lines = [
        f"# 《{city}》{days} 天行程规划",
        "",
        f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"> 住宿：{hotel or '未指定'}",
        f"> 数据来源：{len(profiles)} 个景点" + (f" + {len(foods)} 家餐厅" if foods else "")
        + f"的抖音实地调研 · 共 {total_slots} 个行程点",
        f"> 路线依据：{'高德地图实测（公交线路/票价/打车费用）' if geo_on else 'LLM 交通估算（未配置高德 Key，线路与费用为估算，出发前以地图 App 为准）'}",
        "",
    ]
    # 行程概览卡：总天数/总预算与日均/亮点与避坑数量，一眼看全貌
    ov = build_overview(days, plan, profiles, budget_summary, pitfall, foods)
    spot_line = f"- 总天数 **{ov['days']} 天** ｜ 景点 **{ov['spots']} 个** ｜ 行程点 **{ov['slots']} 个**"
    if ov["foods"]:
        spot_line += f" ｜ 推荐餐厅 **{ov['foods']} 家**"
    lines += ["## 行程概览", "", spot_line]
    if ov["total_cost"] is not None:
        cost_line = (f"- 总预算估算 **约 {ov['total_cost']:.0f} 元** ｜ 每日预算 **约 {ov['daily_cost']:.0f} 元/天**"
                     + (f"（用户预算 {budget_summary['total_budget']:.0f} 元 · {budget_summary['status']}）"
                        if budget_summary["total_budget"] else ""))
        lines.append(cost_line)
    lines += [f"- 亮点 **{ov['highlights']} 条** ｜ 避坑提示 **{ov['pitfalls']} 条**", ""]
    day_costs = day_subtotals(plan, budget_summary)
    if plan.get("summary_note"):
        lines += [f"- 规划说明：{plan['summary_note']}", ""]
    if pitfall:
        lines += ["## 避坑专题（附评论原文）", ""]
        for i, row in enumerate(pitfall, 1):
            lines.append(f"{i}. **{row['claim']}**（{row['confidence']}）")
            if row["quote"]:
                lines.append(f"   > 评论原文：\"{row['quote']}\"")
            if row["source"]:
                lines.append(f"   > 来源：{row['source']}")
        lines.append("")
    for idx, d in enumerate(plan["days"]):
        lines.append(f"## 第 {d['day']} 天")
        lines.append("")
        for s in d["slots"]:
            lines.append(f"### {s['slot']} · {s['spot']}")
            if s["duration"]:
                lines.append(f"- 游玩时长：{s['duration']}")
            if s.get("cost"):
                lines.append(f"- 预估花费：{s['cost']:.0f} 元")
            if s["transport"]:
                lines.append(f"- 交通：{s['transport']}")
            if s["reasons"]:
                lines.append(f"- 值得去：{s['reasons']}")
            if s["notes"]:
                lines.append(f"- 注意事项：{s['notes']}")
            for q in s.get("pitfall_quotes", []):
                lines.append(f"  > 避坑引用：\"{q}\"")
            if s["food"]:
                lines.append(f"- 美食：{s['food']}")
            lines.append("")
        if budget_summary and idx < len(day_costs):
            sc = day_costs[idx]
            lines.append(f"> 当日花费小计：**约 {sc['total']:.0f} 元**"
                         f"（点位花费 {sc['spots']:.0f} + 餐饮 {sc['food']:.0f} + 市内交通 {sc['transport']:.0f}，弹性预留不分摊）")
            lines.append("")
    if heat:
        lines += ["## 热度榜（近 90 天抖音数据）", ""]
        for i, h in enumerate(heat, 1):
            lines.append(f"{i}. **{h['spot']}** ｜ 热度指数 {h['score']:.2f} ｜ {h['trend']}"
                         f"（视频 {h['videos']} 条 · 点赞 {h['likes']} · 评论 {h['comments']}）")
        lines.append("")
    lines.append("## 景点详情卡")
    lines.append("")
    for name, p in profiles.items():
        lines.append(f"### {name}")
        dur = f"约 {p['duration_hours']} 小时" if p["duration_hours"] else "见要点"
        lines.append(f"- 建议时长：{dur}｜最佳时段：{p['best_time_slot']}")
        costs = "；".join(f"{c['item']} {c['amount']:.0f} 元" for c in p.get("cost_items", []))
        if costs:
            lines.append(f"- 参考花费：{costs}")
        if p["highlights"]:
            lines.append(f"- 亮点：{'；'.join(p['highlights'])}")
        if p["avoid"]:
            lines.append(f"- 别踩坑：{'；'.join(p['avoid'])}")
        if p["food"]:
            lines.append(f"- 美食：{'；'.join(p['food'])}")
        if p["photo_spots"]:
            lines.append(f"- 打卡点：{'；'.join(p['photo_spots'])}")
        if p["tips"]:
            lines.append(f"- 贴士：{'；'.join(p['tips'])}")
        dg = (digests or {}).get(name) or {}
        if dg.get("verdict"):
            lines.append(f"- 真实评价摘要：{dg['verdict']}")
            if dg.get("positive"):
                lines.append(f"  - 好评：{dg['positive']}")
            if dg.get("negative"):
                lines.append(f"  - 差评：{dg['negative']}")
            for q in dg.get("quotes", []):
                lines.append(f"  > 评论摘录：\"{q}\"")
        urls = spot_sources.get(name) or []
        if urls:
            links = " ".join(f"[来源{idx + 1}]({u})" for idx, u in enumerate(urls[:6]))
            lines.append(f"- 信息溯源：{links}")
        lines.append("")
    if foods:
        lines.append("## 餐厅详情卡（含避坑分析）")
        lines.append("")
        for name, p in foods.items():
            lines.append(f"### 🍜 {name}")
            costs = "；".join(f"{c['item']} {c['amount']:.0f} 元" for c in p.get("cost_items", []))
            if costs:
                lines.append(f"- 参考花费：{costs}")
            if p["highlights"]:
                lines.append(f"- 招牌/推荐：{'；'.join(p['highlights'])}")
            if p["avoid"]:
                lines.append(f"- 别踩坑：{'；'.join(p['avoid'])}")
            if p["tips"]:
                lines.append(f"- 贴士：{'；'.join(p['tips'])}")
            dg = (digests or {}).get(name) or {}
            if dg.get("verdict"):
                lines.append(f"- 真实评价摘要：{dg['verdict']}")
                if dg.get("positive"):
                    lines.append(f"  - 好评：{dg['positive']}")
                if dg.get("negative"):
                    lines.append(f"  - 差评：{dg['negative']}")
                for q in dg.get("quotes", []):
                    lines.append(f"  > 评论摘录：\"{q}\"")
            urls = (food_sources or {}).get(name) or []
            if urls:
                links = " ".join(f"[来源{idx + 1}]({u})" for idx, u in enumerate(urls[:6]))
                lines.append(f"- 信息溯源：{links}")
            lines.append("")
    if budget_summary:
        lines += ["## 预算明细", "", "| 项目 | 说明 | 金额 |", "|---|---|---|"]
        for row in budget_breakdown({**profiles, **(foods or {})}, budget_summary, days):
            lines.append(f"| {row['item']} | {row['detail']} | {row['amount']:.0f} 元 |")
        lines.append(f"| **预估总计** | 门票+餐饮+交通+弹性 | **{budget_summary['total']:.0f} 元** |")
        if budget_summary["total_budget"]:
            lines.append(f"| 用户预算 | {budget_summary['note']} | {budget_summary['total_budget']:.0f} 元 |")
        lines.append("")
    lines.append("> 本行程由 AI 基于抖音公开内容与地图数据生成，仅供参考；")
    lines.append("> 开放时间、票价、班次等时效信息出发前请务必通过官方渠道核实。")
    return "\n".join(lines)


def render_trip_html(city: str, days: int, hotel: str, plan: dict, profiles: dict[str, dict],
                     spot_sources: dict[str, list[str]], geo_on: bool,
                     locs: dict[str, str] | None = None, budget_summary: dict | None = None,
                     pitfall: list[dict] | None = None, heat: list[dict] | None = None,
                     digests: dict[str, dict] | None = None,
                     foods: dict[str, dict] | None = None,
                     food_sources: dict[str, list[str]] | None = None) -> str:
    """渲染 HTML 可视化路书（Jinja2 模板 + ECharts/Leaflet CDN，离线时模板内置文本版降级）。"""
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template("trip_report.html")
    markers: list[dict] = []
    for name, loc in (locs or {}).items():
        try:
            lng, lat = loc.split(",")
            markers.append({"name": name, "lng": float(lng), "lat": float(lat)})
        except ValueError:
            continue
    return tpl.render(
        city=city, days=days, hotel=hotel, plan=plan, profiles=profiles,
        spot_sources=spot_sources, geo_on=geo_on, markers=markers,
        budget_summary=budget_summary, pitfall=pitfall or [], heat=heat or [],
        digests=digests or {},
        foods=foods or {},
        food_sources=food_sources or {},
        overview=build_overview(days, plan, profiles, budget_summary, pitfall, foods),
        day_costs=day_subtotals(plan, budget_summary),
        breakdown=budget_breakdown({**profiles, **(foods or {})}, budget_summary, days),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_slots=sum(len(d["slots"]) for d in plan["days"]),
    )
