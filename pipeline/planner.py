"""行程规划层：候选圈定 + 景点档案蒸馏 + 行程生成 + 路书渲染（Markdown + HTML）。

与攻略管道的分工：本层零采集，输入全部来自知识库已有的要点，
输出是按时间/空间维度排布的行程路书。所有 LLM 输出都做防御性规范化，
结构不合法宁可降级也不把脏数据传下去。
"""
import re
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import LLM_WEB_SEARCH
from core.llm import chat_json

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

ALLOWED_SLOTS = {"上午", "下午", "晚上", "全天"}
# —— M2b 时间/交通模型常量（PRD F-D1/F-D5）——
CN_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ALL_DAY_HOURS = 5.0            # 建议时长 ≥ 此值视为“全天型”，宜独占一天（F-D1）

# 选点决策表状态图标（F7.1）与质量门禁状态图标（§5.3）
_QC_ICONS = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}

CANDIDATE_SYSTEM = """你是旅行规划师。根据城市与出行天数，列出该城市最值得去的景点名单。
规则：
1. 数量不超过给定上限，优先选标志性、口碑好、适合大众游客的景点；
2. 只输出景点名（不带城市名前缀、不重复、不含餐厅/酒店）；
3. 输出严格 JSON：{"spots": ["..."]}"""

PROFILE_SYSTEM = """你是旅游数据分析师。给定某景点的编号信息要点（含置信度与立场标注），
蒸馏出一份用于行程规划与详情展示的结构化档案。

规则：
1. 只使用给定要点中的信息，禁止用自己的知识补充；某字段没有依据就给空值；
2. duration_hours：预估游玩时长（小时，可为 2.5 这类小数）；要点无依据时给 null；
3. best_time_slot：上午/下午/晚上/全天 四选一，依据要点中的时段建议；无依据给"全天"；
4. avoid 收录"避雷"立场的要点；时效敏感与其他注意事项归入 tips；
5. cost_items：从要点提取的确定花费，每条 {"item": "名称", "type": "门票|餐饮人均|交通|其他", "amount": 数字}；
   只收录要点中有明确数字的花费，估算与无依据的一律不写；无则空数组；
   门票/入园/预约类费用一律 type="门票"，价格给区间时取最低明确数字；
   第三方渠道的代抢/速通/套餐/跟团/包车加价（如“携程899直接买票”“优速通”）不是景区门票，
   禁止写成 type="门票"；页面上的点赞/评论/收藏/分享数字不是价格，禁止当作花费；
   type="餐饮人均" 仅当要点明确给出"人均/每人/一位"的整餐花费时才写；单个菜品或小吃单价
   （如"香辣蟹7元""一碗面12元"）不是人均，归 type="其他"，切勿当成餐饮人均；
6. 每条文本一句话，保留具体事实（数字、地名、时间），不要空泛概括；
7. 输出严格 JSON：{"duration_hours": 数字或null, "best_time_slot": "上午|下午|晚上|全天",
   "highlights": ["..."], "avoid": ["..."], "food": ["..."], "photo_spots": ["..."], "tips": ["..."],
   "cost_items": [{"item": "...", "type": "门票|餐饮人均|交通|其他", "amount": 数字}]}"""

PLAN_SYSTEM = """你是专业行程规划师。基于景点档案与通行时间数据，生成逐日分时段的行程规划。

规则：
1. 必须输出全部 N 天的 days 数组（N=用户要求的天数），一天都不能少：每天分上午/下午/晚上三个时段，
   每时段最多 1 个景点；每天至少两个时段有安排（某点独占整天的“全天型”日子除外），
   不要把行程排得过空，尤其禁止“某天只有一个时段有安排”而其他天很满；
   可排点位不足时也要把它们分散到每一天（例如 3 天只有 4 个点位就排 2+1+1），
   严禁只输出前几天而漏掉后面的整天；
   建议时长 ≥5 小时的全天型景点（主题乐园、远郊大景区等）用唯一一条 slot="全天" 表示，
   禁止把同一天拆成“上午+下午”两条重复点位（系统会按重复剔除，导致当天只剩一个时段）；
   档案 best_time_slot 为"晚上"的景点（夜景/夜游类）必须排在当天晚上时段；
2. 顺路优先：同一天安排通行时间数据中相距近的景点（同区域聚类，避免来回折返）；每天从酒店出发，晚上回酒店附近；
3. 尊重景点的 best_time_slot，尽量把景点排在它最佳的时段；
4. 注意事项分点写：把景点档案中的 avoid 与 tips 逐条拆进 notes 数组，不得省略；
   每条 {"type": "避坑|费用|时间|提示", "text": "一句话"}：avoid/警示类=避坑，
   票价/花费类=费用，开放时间/预约/排队类=时间，其余=提示；
   text 限一句话（不超过 30 字），长内容必须拆成多条；
5. transport 必须具体：给出了交通方案数据的路段，直接引用其中的线路/时长（保留"约"字样）；
   未给出的路段按距离给出大致方案（如"打车约X分钟，具体以地图App为准"），禁止只写"建议查地图"；
6. 行程只规划“去哪玩/怎么去”，不把具体餐厅排进时间线：所有时段的 food 字段一律留空字符串；
7. spot 只能从给定景点名单中选择；每个景点全程只出现一次（某时段没有合适景点就留空，
   绝不能把已排过的景点再排一次，重复排入会被系统剔除）；与用户偏好相符的地标景点
   （如主题乐园、偏好中点名的类型）必须排入行程；
8. reasons 一句话说明为什么值得去（来自档案 highlights）；
9. pitfall_quotes：从该景点档案 avoid 对应的要点中挑最重要的 1~2 条评论原文引用（逐字，不改写）；没有则空数组；
10. 若给了“高赞攻略视频的行程草案”，以草案为主干安排：草案里的点位优先排入并尽量保留其先后顺序，
    天数不符时按档案的时长/最佳时段增删调整；草案点不在景点档案里说明未被验证通过，不得排入；
    没有草案时按档案与通行数据自行编排；
11. summary_note：若有调研过但未排入行程的景点，必须把它们列为"备选"并一句话说明原因；无则空字符串；
12. 输出严格 JSON：{"summary_note": "...", "days": [{"day": 1, "slots": [{"slot": "上午|下午|晚上|全天", "spot": "景点名",
   "duration": "约X小时", "transport": "从上一地点至此的方式与耗时",
   "reasons": "...", "notes": [{"type": "避坑|费用|时间|提示", "text": "一句话"}], "food": "", "pitfall_quotes": ["评论原文"]}]}]}"""

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


# 第三方渠道加价/代抢/套餐价不是景区门票价（如评论“携程899直接买票，全程速通”）。
# 这类数字一旦被当成门票，既会造成离谱票价，也会与详情卡口径分裂（R12 两处同源）。
_THIRD_PARTY_PRICE_WORDS = (
    "携程", "飞猪", "美团", "去哪儿", "同程", "代抢", "代购", "代订", "速通",
    "黄牛", "套餐", "一日游", "跟团", "包车", "接送", "预约费", "服务费", "加急", "讲解",
)


def _is_third_party_price(item: str) -> bool:
    """名称里带第三方渠道/代抢/附加服务字样 → 不是景区门票本体价。"""
    return any(w in (item or "") for w in _THIRD_PARTY_PRICE_WORDS)


# 园内商品/小吃/单独收费项目也不是门票本体价（实测：评论“一根烤肠卖12”被蒸馏成花费项后，
# 一旦当成门票价，详情卡会给出离谱票价）。
_RETAIL_PRICE_WORDS = (
    "烤肠", "香肠", "小吃", "零食", "饮料", "矿泉水", "冰淇淋", "雪糕", "奶茶", "咖啡",
    "纪念品", "伴手礼", "特产", "明信片", "文创", "周边", "玩具", "雨衣", "雨伞",
    "停车", "观光车", "摆渡车", "索道", "缆车", "游船", "船票", "寄存", "童车", "轮椅",
)


def _is_retail_price(item: str) -> bool:
    """名称是园内商品/小吃/单独收费项目 → 不是景区门票本体价。"""
    return any(w in (item or "") for w in _RETAIL_PRICE_WORDS)


def _is_non_ticket(item: str) -> bool:
    """门票口径的统一排除判定：第三方加价 + 园内商品，两者都不进门票池与详情卡票价。"""
    return _is_third_party_price(item) or _is_retail_price(item)


def pick_ticket_price(cost_items) -> float | None:
    """门票唯一口径（catalog 详情与行程槽位共用，R12 两处同源的取数源头）。

    规则：剔除第三方渠道代抢/速通/套餐加价项；多条门票价取最低明确数字（与 PROFILE_SYSTEM
    规则 5“区间取最低”一致）；显式 0 视为“免费”返回 0.0；无可靠门票价返回 None（渲染标待核实）。
    纯函数，独立可测。"""
    prices: list[float] = []
    free = False
    for c in cost_items or []:
        if not isinstance(c, dict) or str(c.get("type") or "") not in ("门票", "票价"):
            continue
        if _is_non_ticket(str(c.get("item") or "")):
            continue
        try:
            v = float(c.get("amount"))
        except (TypeError, ValueError):
            continue
        if v > 0:
            prices.append(round(v, 2))
        elif v == 0:
            free = True
    if prices:
        return min(prices)
    return 0.0 if free else None


# 门票唯一口径 pick_ticket_price（上方）供 catalog 详情卡读权威价；
# 点位 cost 溯源与预算池的旧兜底链（P6）已随预算模块一并移除：报告不再输出任何金额估算。


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
    # 花费项：只保留名称与金额都合法且金额>0 的条目（详情卡与门票口径的原料，脏数据一律丢弃）
    cost_items = []
    for x in data.get("cost_items") or []:
        if not isinstance(x, dict):
            continue
        item = str(x.get("item") or "").strip()
        ctype = str(x.get("type") or "").strip()
        if ctype in ("门票", "票价") and _is_non_ticket(item):
            continue   # 代抢/速通/套餐加价与园内小吃商品（如“烤肠12元”）都不是景区门票，不得进门票口径与详情卡
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
        "is_all_day": (dur is not None and dur >= ALL_DAY_HOURS) or data.get("is_all_day") is True,
        "highlights": lst("highlights"),
        "avoid": lst("avoid"),
        "food": lst("food"),
        "photo_spots": lst("photo_spots"),
        "tips": lst("tips"),
        "cost_items": cost_items,
    }


def empty_profile() -> dict:
    return {
        "duration_hours": None, "best_time_slot": "全天", "is_all_day": False,
        "highlights": [], "avoid": [], "food": [], "photo_spots": [], "tips": [],
        "cost_items": [],
    }


def _conf_badge(p: dict) -> str:
    """量化置信度徽章：高/中/低置信度 + 独立来源数（未标注时回退语义标签）。"""
    level = p.get("conf_level")
    if not level:
        return p.get("confidence", "单源")
    n = p.get("n_sources") or 1
    return f"{level}·{n}来源" if n > 1 else level


def build_spot_profile(spot: str, points: list[dict]) -> dict:
    """从某景点的全部要点蒸馏结构化档案（单次 LLM 调用）。"""
    numbered = "\n".join(
        f"[{i + 1}] ({p.get('topic', '其他')}|{_conf_badge(p)}|{p.get('stance', '中性')}) {p['claim']}"
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
        f"[{i + 1}] ({p.get('stance', '中性')}|{_conf_badge(p)}) {p['claim']}"
        + (f"（评论原文：{p['quote']}）" if p.get("quote") else "")
        for i, p in enumerate(points)
    )
    data = chat_json(DIGEST_SYSTEM, f"景点：{spot}\n信息要点：\n{numbered or '(无)'}")
    return _normalize_digest(data, quote_pool)


# —— 注意事项分点化：类型图标 + 关键词兜底分类（纯函数，独立可测）——
NOTE_TYPES = {"避坑", "费用", "时间", "提示"}

_PIT_RE = re.compile(r"别|不要|禁止|严禁|注意|当[心小]|避[雷坑]|勿|坑|劝退|不值|不划算|警惕")
_COST_RE = re.compile(r"\d+\s*元|门票|收费|免费|人均|费用|押金|价格|价钱")
_TIME_RE = re.compile(r"\d+\s*[点时分]|开放|闭[馆园门]|周[一二三四五六日末]|节假日|预约|排队|早[上晨]|夜场|旺季|淡季")


def classify_note(text: str) -> str:
    """注意事项类型兜底分类（LLM 未给或给错类型时按关键词判）：避坑 > 费用 > 时间 > 提示。"""
    if _PIT_RE.search(text):
        return "避坑"
    if _COST_RE.search(text):
        return "费用"
    if _TIME_RE.search(text):
        return "时间"
    return "提示"


def normalize_notes(raw) -> list[dict]:
    """notes 统一成 [{"type", "text"}] 分点列表：兼容旧版字符串（按；/换行拆分）、
    字符串列表、字典列表；类型非法时用 classify_note 兜底。纯函数可测。"""
    items: list[dict] = []
    if isinstance(raw, str):
        parts = [x.strip() for x in re.split(r"[；;\n]", raw) if x.strip()]
        items = [{"text": p} for p in parts]
    elif isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict) and str(x.get("text") or "").strip():
                items.append({"text": str(x["text"]).strip(), "type": x.get("type")})
            elif isinstance(x, str) and x.strip():
                items.append({"text": x.strip()})
    out = []
    for it in items:
        t = str(it.get("type") or "").strip()
        out.append({"type": t if t in NOTE_TYPES else classify_note(it["text"]), "text": it["text"]})
    return out


def _normalize_plan(data: dict, allowed_spots: set[str], days: int) -> dict:
    """行程防御性规范化：丢弃不在名单中的景点、修正非法时段、限制天数、跨天去重。纯函数可测。

    每个景点全程只排一次（PLAN_SYSTEM 规则 7）：LLM 偶尔把同一点排进多天（如九龙壁第 1、2 天各一次），
    这里按首次出现保留、后续重复丢弃并记入 duplicate_drops，供 QC 与报告明示（不静默）。"""
    out_days = []
    seen_spots: set[str] = set()
    duplicate_drops: list[str] = []
    for d in (data.get("days") or [])[:days]:
        slots = []
        for s in d.get("slots") or []:
            spot = str(s.get("spot") or "").strip()
            slot = str(s.get("slot") or "").strip()
            if not spot or spot not in allowed_spots:
                continue
            if spot in seen_spots:
                duplicate_drops.append(f"{spot}（第{len(out_days) + 1}天{slot or '未标时段'}）")
                continue
            seen_spots.add(spot)
            quotes = s.get("pitfall_quotes")
            quotes = [str(q).strip()[:80] for q in quotes if str(q).strip()] if isinstance(quotes, list) else []
            slots.append(
                {
                    "slot": slot if slot in ALLOWED_SLOTS else "下午",
                    "spot": spot,
                    "duration": str(s.get("duration") or "").strip(),
                    "transport": str(s.get("transport") or "").strip(),
                    "reasons": str(s.get("reasons") or "").strip(),
                    "notes": normalize_notes(s.get("notes")),
                    "food": str(s.get("food") or "").strip(),
                    "pitfall_quotes": quotes,
                }
            )
        if slots:
            out_days.append({"day": len(out_days) + 1, "slots": slots})
    return {"days": out_days, "summary_note": str(data.get("summary_note") or "").strip(),
            "duplicate_drops": duplicate_drops}


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


# ============================ M2b：时间模型 + 结构化交通 Leg ============================
def day_weekdays_from(start_date: str | None, days: int) -> list[str]:
    """由出发日推算每日星期（中文）。无日期/非法格式返回 []（R3 闭馆校验据此降级为不判定）。纯函数可测。"""
    if not start_date:
        return []
    s = str(start_date).strip()[:10]
    d0 = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            d0 = datetime.strptime(s, fmt).date()
            break
        except ValueError:
            continue
    if d0 is None:
        return []
    return [CN_WEEKDAYS[(d0 + timedelta(days=i)).weekday()] for i in range(max(1, int(days)))]


def is_all_day(profile: dict | None) -> bool:
    """是否“全天型”点位：优先看已存标位，否则由建议时长判定（≥ALL_DAY_HOURS）。纯函数可测。"""
    p = profile or {}
    if p.get("is_all_day") is True:
        return True
    try:
        return p.get("duration_hours") is not None and float(p["duration_hours"]) >= ALL_DAY_HOURS
    except (TypeError, ValueError):
        return False


def _speed_kmh(mode: str) -> float:
    m = str(mode or "")
    if "步行" in m:
        return 4.5
    if "地铁" in m:
        return 32.0
    if "公交" in m:
        return 18.0
    return 25.0  # 打车/自驾默认


def _match_hint(a: str, b: str, travel_lines: list[str] | None) -> str:
    for line in travel_lines or []:
        if "->" in line and line.split(":", 1)[0].strip() == f"{a}->{b}":
            return line.split(":", 1)[1].strip()
    return ""


def _make_leg(a: str, b: str, locs: dict[str, str], travel_lines: list[str] | None) -> dict:
    """单段交通 Leg：有坐标优先高德 travel_time（实测），无则按距离与方式估算。

    只给方式与耗时（行程信息），不含任何费用估算——报告不输出金额数字。"""
    from core import geo
    la, lb = locs.get(a, ""), locs.get(b, "")
    km = None
    if la and lb:
        try:
            km = geo.distance_km(la, lb)
        except Exception:
            km = None
    minutes, mode, nature = None, "", "估算"
    if la and lb and geo.available():
        try:
            tt = geo.travel_time(la, lb)
        except Exception:
            tt = None
        if tt:
            minutes, mode = tt[0], tt[1]
            nature = "实测"
    if not mode:
        mode = "步行" if (km is not None and km < 1.5) else "公交"
    if minutes is None and km is not None:
        minutes = int(round(km / _speed_kmh(mode) * 60))
    return {"from": a, "to": b, "mode": mode, "minutes": minutes,
            "km": round(km, 2) if km is not None else None,
            "note": _match_hint(a, b, travel_lines), "nature": nature}


def build_legs(city: str, hotel: str, plan: dict, locs: dict[str, str],
               travel_lines: list[str] | None = None) -> list[dict]:
    """为已排入行程的相邻停留点（含酒店往返）生成结构化交通 Leg（F-D5）。纯构造、可测。"""
    hl = locs.get("酒店") or (locs.get(hotel) if hotel else "")
    use_hotel = bool(hl or (hotel and hotel.strip()))   # 给了酒店名即使无坐标也补酒店往返段（估算）
    legs: list[dict] = []
    for day in plan.get("days", []):
        stops = [s.get("spot") for s in day.get("slots", []) if s.get("spot")]
        if not stops:
            continue
        seq = (["酒店"] if use_hotel else []) + stops + (["酒店"] if use_hotel else [])
        dn = day.get("day")
        for a, b in zip(seq, seq[1:]):
            leg = _make_leg(a, b, locs, travel_lines)
            leg["day"] = dn
            legs.append(leg)
    return legs


def plan_itinerary(city: str, days: int, hotel: str, profiles: dict[str, dict],
                   travel_lines: list[str], preferences: str,
                   preference_mode: str = "均衡",
                   extra_issues: list[str] | None = None,
                   heat_summary: str = "",
                   guide_hints: list[str] | None = None,
                   draft_plan: dict | None = None) -> dict:
    """一次 LLM 调用生成行程 JSON，随后做防御性规范化（排布兜底见 rebalance_days）。

    extra_issues：来自 pipeline.qc 权威门禁的修正指令（F5.2 有限回炉）。传入时连同覆盖率
    问题一起写进回炉提示；采纳条件放宽为"覆盖率不变差即返回重试版"，最终是否采纳由调用方
    用 qc.problem_count 权威判定（无 extra_issues 时保持原有"严格变少才采纳"行为）。

    guide_hints（M6-B）：城市高赞攻略视频提炼出的真实编排建议（串线顺序/住宿片区/
    可跳过项）。传入时拼进提示词，使排线有实证依据而非只靠模型基线常识；
    不传则行为与从前完全一致（零回归）。

    draft_plan（M6-C）：高赞攻略视频的行程草案（已由 LLM 审核增删改）——传入时作为主干
    注入提示词，优先保留视频实证的点位与顺序；不传则按景点档案自行编排。"""
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
    user = (
        f"城市：{city}\n天数：{days}\n住宿酒店：{hotel or '未指定'}\n"
        f"消费偏好：{preference_mode}\n"
        f"用户偏好：{preferences or '无'}\n\n"
        f"景点档案：\n" + "\n".join(profile_lines) + "\n\n"
        f"交通方案数据（实测或估算，transport 优先引用）：\n"
        + ("\n".join(travel_lines) if travel_lines else "（无，按距离给出大致方案并标注'以地图App为准'）")
    )
    # F-A4：热度与证据在规划前已算好并喂入，引导优先安排证据强、口碑好的点
    if heat_summary:
        user += "\n\n已知热度与证据（优先排入证据强、热度高且趋势向好的点）：\n" + heat_summary
    # M6-B：城市攻略层的真实编排知识（来自高赞攻略视频，不是模型常识）——
    # 串线顺序/住宿片区/可跳过项据此判断；与景点档案冲突时以档案的实测数据为准
    if guide_hints:
        hints = [str(h).strip() for h in guide_hints if str(h).strip()][:10]
        if hints:
            user += ("\n\n真实攻略的编排建议（来自该城市高赞攻略视频，优先参考；"
                     "与上述景点档案冲突时以档案为准）：\n"
                     + "\n".join(f"- {h}" for h in hints))
    # M6-C：高赞攻略视频的行程草案（已审核增删改）作为主干注入——用户要的就是
    # “先看热门视频怎么排，再逐点验证”，草案优先于模型自行编排
    if draft_plan:
        draft_txt = format_draft_plan(draft_plan)
        if draft_txt:
            user += ("\n\n高赞攻略视频的行程草案（真实攻略里被反复验证的编排，作为主干优先采用；"
                     "点位顺序尽量保留，天数不符时按档案的建议时长/最佳时段增删）：\n" + draft_txt)
    data = chat_json(PLAN_SYSTEM, user)
    plan = _normalize_plan(data, set(profiles.keys()), days)
    # 覆盖率兜底 + 质量门禁回炉：排点过少/晚上型错位/未排景点无备选说明（_coverage_issues），
    # 以及来自 pipeline.qc 的权威门禁指令（extra_issues），一起写进回炉提示重试一次。
    base_issues = _coverage_issues(plan, profiles, days)
    extra = [str(x).strip() for x in (extra_issues or []) if str(x).strip()]
    if base_issues or extra:
        fix_note = ("\n\n上一版规划存在以下问题，本版必须修正：\n"
                    + "\n".join(f"- {x}" for x in base_issues + extra))
        data2 = chat_json(PLAN_SYSTEM, user + fix_note)
        plan2 = _normalize_plan(data2, set(profiles.keys()), days)
        base2 = len(_coverage_issues(plan2, profiles, days))
        # 无外部门禁指令：覆盖率问题必须严格变少才采纳（原有行为）；
        # 有门禁指令：覆盖率不变差即返回重试版，权威采纳交调用方（qc.problem_count 比较）
        if base2 < len(base_issues) or (extra and base2 <= len(base_issues)):
            plan = plan2
    # 餐饮解耦（PRD §9.3）：行程时间线不排具体餐厅，一律清空 slot.food（传空候选集→全部置空）；
    # 餐厅调研结果仅供美食推荐榜（M4）与详情卡使用
    plan = _filter_fabricated_food(plan, set())
    # 排布兜底：LLM 排出“某天只有 1 个时段”时，把过满天的点位移过来（确定性修复）
    return rebalance_days(plan, profiles, days)


def _filter_fabricated_food(plan: dict, food_names: set[str]) -> dict:
    """清空未命中餐厅候选的 food 字段（LLM 无候选时会自行编店名）。纯函数可测。"""
    out = dict(plan)
    out["days"] = [
        {**d, "slots": [
            {**s, "food": s.get("food", "") if any(n in str(s.get("food", "")) for n in food_names) else ""}
            for s in d.get("slots", [])]}
        for d in plan.get("days", [])
    ]
    return out


def _coverage_issues(plan: dict, profiles: dict[str, dict], days: int) -> list[str]:
    """规划覆盖率检查（纯函数可测）：天数不完整、行程点过少、晚上型景点错位、未排景点无备选说明。

    不要求全排入：景点多天数少时有备选是正常的，关键是地标不能被静默丢掉。"""
    slots = [s for d in plan.get("days", []) for s in d.get("slots", [])]
    issues: list[str] = []
    # 天数完整性（比点位数更硬）：LLM 偶尔少输出整天，PLAN_SYSTEM 规则 1 已明写要求
    # 但仍会被违反（实测：要 3 天只给 2 天），提示词约束必须配代码兜底才能触发回炉
    plan_days = plan.get("days") or []
    empty_days = [i + 1 for i, dd in enumerate(plan_days) if not (dd.get("slots") or [])]
    if len(plan_days) < days or empty_days:
        tail = f"，其中第 {'、'.join(str(x) for x in empty_days)} 天无任何安排" if empty_days else ""
        issues.append(
            f"行程天数不完整：要求 {days} 天，实际只有 {len(plan_days)} 天{tail}。"
            f"必须输出全部 {days} 天、每天至少一个时段有安排；"
            f"点位不足时把已调研点分散到每一天，不得空整天")
    min_slots = min(2 * days, len(profiles))
    if len(slots) < min_slots:
        issues.append(f"行程点只有 {len(slots)} 个，低于下限 {min_slots} 个，请把更多已调研景点排入行程")
    # 每日密度（实测出现过“第 2 天只有上午”）：除“全天型独日”外，每天至少两个时段有安排；
    # 只在素材够分时提要求（点数本来就不够摊时不逼回炉，交由 rebalance_days 与已知妥协）
    all_day_names = {n for n, p in profiles.items() if is_all_day(p)}
    all_day_days, thin_days = 0, []
    for i, dd in enumerate(plan_days, start=1):
        sl = dd.get("slots") or []
        if len(sl) == 1 and str(sl[0].get("spot") or "") in all_day_names:
            all_day_days += 1   # 全天型点独占一天：单条 slot="全天" 为合法排法（F-D1）
        elif len(sl) <= 1:
            thin_days.append(i)
    if thin_days and len(slots) >= 2 * len(plan_days) - all_day_days:
        issues.append(f"排布过空：第 {'、'.join(str(x) for x in thin_days)} 天只有 1 个时段有安排。"
                      f"除全天型景点独占一整天外，每天上午/下午/晚上至少要有两个时段有安排，"
                      f"请重新均衡分配，不要出现“某天只有上午”这类残缺日子")
    planned = {s["spot"] for s in slots}
    for name in planned:
        p = profiles.get(name) or {}
        if p.get("best_time_slot") == "晚上" and not any(
                s["spot"] == name and s["slot"] == "晚上" for s in slots):
            issues.append(f"{name} 的最佳时段是晚上，却没有排在晚上时段，必须调整到晚上")
    unplanned = [n for n in profiles if n not in planned]
    if unplanned and "备选" not in str(plan.get("summary_note", "")):
        issues.append(f"未排入的景点：{'、'.join(unplanned)}——与用户偏好相符的地标景点（如主题乐园）必须排入，"
                      "确实排不下的在 summary_note 中列为备选并说明原因")
    return issues


def format_draft_plan(draft: dict | None) -> str:
    """把“视频行程草案”格式化成规划提示词文本（纯函数可测）；无有效内容返回空串。"""
    if not isinstance(draft, dict):
        return ""
    lines: list[str] = []
    for d in draft.get("days") or []:
        if not isinstance(d, dict):
            continue
        bits: list[str] = []
        for it in d.get("slots") or d.get("items") or []:
            if not isinstance(it, dict):
                continue
            spot = str(it.get("spot") or "").strip()
            if not spot:
                continue
            slot = str(it.get("slot") or it.get("period") or "").strip()
            bits.append(f"{slot} {spot}" if slot else spot)
        if bits:
            lines.append(f"第 {d.get('day') or len(lines) + 1} 天：" + "；".join(bits))
    note = str(draft.get("notes") or "").strip()
    if note:
        lines.append(f"（审核说明：{note}）")
    return "\n".join(lines)


# 时段顺序（搬移后重排当天槽位用）
_SLOT_ORDER = {"上午": 0, "下午": 1, "晚上": 2, "全天": 0}


def _slot_ok(block: dict, profiles: dict[str, dict], slot: str) -> bool:
    """搬移兼容性：晚上型景点只能落在晚上时段、全天型点不参与搬移。纯函数可测。"""
    p = profiles.get(str(block.get("spot") or "")) or {}
    if is_all_day(p):
        return False
    return not (str(p.get("best_time_slot") or "") == "晚上" and slot != "晚上")


def rebalance_days(plan: dict, profiles: dict[str, dict], days: int) -> dict:
    """排布均衡兜底（纯函数可测）：把排得过满的天里的点位挪到只有 1 个时段的天，
    使除“全天型独占日”外每天至少两个时段有安排（实测出现过“第 2 天只有上午”）。

    只在素材够分（已排点数 ≥ 2×天数 − 全天独占日数）时移动；不新增/删除点位，
    仅调整点位的归属日与时段标签，并保持晚上型点落在晚上。返回新 plan（不改入参），
    moved 记录每次移动供报告明示。"""
    out = {**plan,
           "days": [dict(d, slots=list(d.get("slots") or [])) for d in plan.get("days") or []],
           "moved": []}
    days_list = out["days"]

    def _all_day_only(dd: dict) -> bool:
        sl = dd.get("slots") or []
        return len(sl) == 1 and is_all_day(profiles.get(str(sl[0].get("spot") or "")))

    all_day_days = sum(1 for dd in days_list if _all_day_only(dd))
    n_slots = sum(len(dd.get("slots") or []) for dd in days_list)
    if n_slots < 2 * len(days_list) - all_day_days:
        return out   # 素材本来就不够摊（如 3 天 4 个点）：不硬凑，交由回炉与已知妥协说明
    while True:
        thin = [dd for dd in days_list if len(dd.get("slots") or []) <= 1 and not _all_day_only(dd)]
        donors = [dd for dd in days_list if len(dd.get("slots") or []) >= 3]
        if not thin or not donors:
            break
        dest = thin[0]
        donor = max(donors, key=lambda dd: len(dd["slots"]))
        taken = {str(s.get("slot") or "") for s in dest["slots"]}
        moved_one = False
        for free in (t for t in ("上午", "下午", "晚上") if t not in taken):
            pool = [s for s in donor["slots"] if _slot_ok(s, profiles, free)]
            if not pool:
                continue
            move = pool[-1]
            donor["slots"].remove(move)
            move["slot"] = free
            dest["slots"].append(move)
            dest["slots"].sort(key=lambda s: _SLOT_ORDER.get(str(s.get("slot") or ""), 3))
            out["moved"].append(f"第{donor.get('day')}天 {move.get('spot')} → 第{dest.get('day')}天{free}")
            moved_one = True
            break
        if not moved_one:
            break   # 目标天可选时段里没有兼容的点位（如全是晚上型）：不硬搬，保持原样
    return out


def build_overview(days: int, plan: dict, profiles: dict[str, dict],
                   pitfall: list[dict] | None,
                   foods: dict[str, dict] | None = None) -> dict:
    """行程概览卡数据：天数/景点与餐厅数/行程点数/亮点与避坑数量。纯函数可测。"""
    total_slots = sum(len(d.get("slots", [])) for d in plan.get("days", []))
    return {
        "days": days,
        "spots": len(profiles),
        "foods": len(foods or {}),
        "slots": total_slots,
        "highlights": sum(len(p.get("highlights", [])) for p in profiles.values()),
        "pitfalls": len(pitfall or []),
    }
