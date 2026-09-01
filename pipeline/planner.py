"""行程规划层：候选圈定 + 景点档案蒸馏 + 行程生成 + 路书渲染。

与攻略管道的分工：本层零采集，输入全部来自知识库已有的要点，
输出是按时间/空间维度排布的行程路书。所有 LLM 输出都做防御性规范化，
结构不合法宁可降级也不把脏数据传下去。
"""
from datetime import datetime

from core.llm import chat_json

ALLOWED_SLOTS = {"上午", "下午", "晚上"}

CANDIDATE_SYSTEM = """你是旅行规划师。根据城市与出行天数，列出该城市最值得去的景点名单。
规则：
1. 数量不超过给定上限，优先选标志性、口碑好、适合大众游客的景点；
2. 只输出景点名（不带城市名前缀、不重复、不含餐厅/酒店）；
3. 输出严格 JSON：{"spots": ["..."]}"""

PROFILE_SYSTEM = """你是旅游数据分析师。给定某景点的编号信息要点（含置信度与立场标注），
蒸馏出一份用于行程规划的结构化档案。

规则：
1. 只使用给定要点中的信息，禁止用自己的知识补充；某字段没有依据就给空值；
2. duration_hours：预估游玩时长（小时，可为 2.5 这类小数）；要点无依据时给 null；
3. best_time_slot：上午/下午/晚上/全天 四选一，依据要点中的时段建议；无依据给"全天"；
4. avoid 收录"避雷"立场的要点；时效敏感与其他注意事项归入 tips；
5. 每条文本一句话，保留具体事实（数字、地名、时间），不要空泛概括；
6. 输出严格 JSON：{"duration_hours": 数字或null, "best_time_slot": "上午|下午|晚上|全天",
   "highlights": ["..."], "avoid": ["..."], "food": ["..."], "photo_spots": ["..."], "tips": ["..."]}"""

PLAN_SYSTEM = """你是专业行程规划师。基于景点档案与通行时间数据，生成逐日分时段的行程规划。

规则：
1. 每天分上午/下午/晚上三个时段，每时段安排 0~2 个景点（行程要留白，不要排满）；
2. 顺路优先：同一天安排通行时间数据中相距近的景点；每天从酒店出发，晚上回酒店附近；
3. 尊重景点的 best_time_slot，尽量把景点排在它最佳的时段；
4. 景点档案中的 avoid 与 tips 必须完整写进该景点的 notes，不得省略；
5. 给出了通行时间数据的路段，transport 必须引用真实数据（如"公交约40分钟"）；没给出的写"建议查地图"；
6. 每天的晚上时段若景点档案有 food 信息，把美食建议写进 food 字段；
7. spot 只能从给定景点名单中选择；每个景点全程只出现一次；
8. reasons 一句话说明为什么值得去（来自档案 highlights）；
9. 输出严格 JSON：{"days": [{"day": 1, "slots": [{"slot": "上午|下午|晚上", "spot": "景点名",
   "duration": "约X小时", "transport": "从上一地点至此的方式与耗时", "reasons": "...",
   "notes": "...", "food": ""}]}]}"""


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
    return {
        "duration_hours": dur,
        "best_time_slot": slot if slot in ALLOWED_SLOTS | {"全天"} else "全天",
        "highlights": lst("highlights"),
        "avoid": lst("avoid"),
        "food": lst("food"),
        "photo_spots": lst("photo_spots"),
        "tips": lst("tips"),
    }


def empty_profile() -> dict:
    return {
        "duration_hours": None, "best_time_slot": "全天",
        "highlights": [], "avoid": [], "food": [], "photo_spots": [], "tips": [],
    }


def build_spot_profile(spot: str, points: list[dict]) -> dict:
    """从某景点的全部要点蒸馏结构化档案（单次 LLM 调用）。"""
    numbered = "\n".join(
        f"[{i + 1}] ({p.get('topic', '其他')}|{p.get('confidence', '单源')}|{p.get('stance', '中性')}) {p['claim']}"
        for i, p in enumerate(points)
    )
    data = chat_json(PROFILE_SYSTEM, f"景点：{spot}\n信息要点：\n{numbered or '(无)'}")
    return _normalize_profile(data)


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
            slots.append(
                {
                    "slot": slot if slot in ALLOWED_SLOTS else "下午",
                    "spot": spot,
                    "duration": str(s.get("duration") or "").strip(),
                    "transport": str(s.get("transport") or "").strip(),
                    "reasons": str(s.get("reasons") or "").strip(),
                    "notes": str(s.get("notes") or "").strip(),
                    "food": str(s.get("food") or "").strip(),
                }
            )
        if slots:
            out_days.append({"day": len(out_days) + 1, "slots": slots})
    return {"days": out_days}


def plan_itinerary(city: str, days: int, hotel: str, profiles: dict[str, dict],
                   travel_lines: list[str], preferences: str) -> dict:
    """一次 LLM 调用生成行程 JSON，随后做防御性规范化。"""
    profile_lines = []
    for name, p in profiles.items():
        dur = f"约{p['duration_hours']}小时" if p["duration_hours"] else "时长未知"
        profile_lines.append(
            f"【{name}】最佳时段:{p['best_time_slot']} | 建议时长:{dur}\n"
            f"  亮点: {'; '.join(p['highlights']) or '无'}\n"
            f"  避雷: {'; '.join(p['avoid']) or '无'}\n"
            f"  美食: {'; '.join(p['food']) or '无'}\n"
            f"  注意: {'; '.join(p['tips']) or '无'}"
        )
    user = (
        f"城市：{city}\n天数：{days}\n住宿酒店：{hotel or '未指定'}\n"
        f"用户偏好：{preferences or '无'}\n\n"
        f"景点档案：\n" + "\n".join(profile_lines) + "\n\n"
        f"通行时间数据（高德实测）：\n" + ("\n".join(travel_lines) if travel_lines else "（无，请按区域常识排线）")
    )
    data = chat_json(PLAN_SYSTEM, user)
    return _normalize_plan(data, set(profiles.keys()), days)


def render_trip(city: str, days: int, hotel: str, plan: dict, profiles: dict[str, dict],
                spot_sources: dict[str, list[str]], geo_on: bool) -> str:
    """把行程 JSON 渲染成 Markdown 路书（逐日卡片 + 景点详情 + 来源链接）。"""
    total_slots = sum(len(d["slots"]) for d in plan["days"])
    lines = [
        f"# 《{city}》{days} 天行程规划",
        "",
        f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"> 住宿：{hotel or '未指定'}",
        f"> 数据来源：{len(profiles)} 个景点的抖音实地调研 · 共 {total_slots} 个行程点",
        f"> 路线依据：{'高德地图实测通行时间' if geo_on else 'LLM 区域推断（未配置高德 Key，顺路精度有限）'}",
        "",
    ]
    for d in plan["days"]:
        lines.append(f"## 第 {d['day']} 天")
        lines.append("")
        for s in d["slots"]:
            lines.append(f"### {s['slot']} · {s['spot']}")
            if s["duration"]:
                lines.append(f"- 游玩时长：{s['duration']}")
            if s["transport"]:
                lines.append(f"- 交通：{s['transport']}")
            if s["reasons"]:
                lines.append(f"- 值得去：{s['reasons']}")
            if s["notes"]:
                lines.append(f"- 注意事项：{s['notes']}")
            if s["food"]:
                lines.append(f"- 美食：{s['food']}")
            lines.append("")
    lines.append("## 景点详情卡")
    lines.append("")
    for name, p in profiles.items():
        lines.append(f"### {name}")
        dur = f"约 {p['duration_hours']} 小时" if p["duration_hours"] else "见要点"
        lines.append(f"- 建议时长：{dur}｜最佳时段：{p['best_time_slot']}")
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
        urls = spot_sources.get(name) or []
        if urls:
            links = " ".join(f"[来源{idx + 1}]({u})" for idx, u in enumerate(urls[:6]))
            lines.append(f"- 信息溯源：{links}")
        lines.append("")
    lines.append("> 本行程由 AI 基于抖音公开内容与地图数据生成，仅供参考；")
    lines.append("> 开放时间、票价、班次等时效信息出发前请务必通过官方渠道核实。")
    return "\n".join(lines)
