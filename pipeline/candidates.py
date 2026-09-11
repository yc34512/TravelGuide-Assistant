"""混合候选生成与验证筛选（P0 核心）。

链路：大模型圈定 15~20 个候选（景点/美食/体验/购物）→ 逐个抖音验证采集
（由 service.trip 驱动，命中缓存免采）→ 大模型交叉验证筛选出 8~12 个优质候选。

营销号过滤：视频文案命中营销话术正则即被标记，不参与候选评分——
本 P0 数据基线以"文案 + 评论"为主，文案的首要价值就是存在性验证与营销号识别。
"""
from config import LLM_WEB_SEARCH
from core.llm import chat_json
# 营销号识别上移到 core.quality（平台中立层）：候选筛选、置信度降级、热度榜营销号占比、
# 采集质量闸四处共用同一份正则口径（不再各写一份）。此处 re-export 保持既有
# `from pipeline.candidates import is_marketing` 的全部引用零改动。
from core.quality import MARKETING_RE, is_marketing   # noqa: F401  (re-export)

# 类别配额：景点是行程骨架必须充足，美食/体验点缀，购物少量点睛
CATEGORY_QUOTA = {"景点": 10, "美食": 5, "体验": 3, "购物": 2}
TOTAL_CANDIDATES_MAX = 20
VERIFY_MAX = 12          # 验证采集的候选数硬上限（成本控制闸）
# 验证阶段的类别配额：景点是行程骨架占大头，但美食/体验/购物保底进验证，
# 避免 LLM 返回顺序靠后的类别被 [:VERIFY_MAX] 整体截断成"未定"（F2.1 公平截断）
VERIFY_QUOTA = {"景点": 6, "美食": 3, "体验": 2, "购物": 1}
KEEP_MIN, KEEP_MAX = 8, 12

GEN_SYSTEM = """你是旅行候选圈定专家。为指定城市生成值得实地验证的候选清单。

规则：
1. 总数 15~20 个，按类别配额：景点约10、美食约5、体验约3、购物约2（购物仅列 1~2 个代表性场所）；
2. 候选要具体到可直接搜索的名称（如"云冈石窟"而非"古建筑"；"凤临阁"而非"好吃的"）；
3. 优先选游客真实会去、有讨论热度的，避免冷门到搜不到内容的；结合近期真实热度与开放情况（如有联网信息可参考）；
4. 结合用户偏好与天数调整侧重（如天数短则砍购物/体验）；
   若输入给了"视频行程草案点位"，这些点位必须全部纳入候选；
5. 输出严格 JSON：{"candidates": [{"name": "...", "category": "景点|美食|体验|购物", "reason": "一句话理由"}]}"""

# 攻略层产物上限（成本与提示词长度闸）
GUIDE_MENTION_MAX = 20         # 提炼的候选点上限
GUIDE_HINTS_MAX = 12           # 编排建议上限
GUIDE_ITINERARY_MAX = 3        # 保留的视频行程草案条数上限（审核输入长度闸）
GUIDE_COMMENTS_PER_VIDEO = 15  # 每条视频取的高赞评论数

# 城市攻略层（M6-B）：从"{城市}旅游攻略/N天N夜"这类综合攻略视频里读真实编排知识。
# 从前圈定完全靠 LLM 凭空想象（一次采集都没有），城市级的"几天合适/住哪/怎么串线/
# 哪个点可以跳过"根本没有数据来源——这正是报告"泛泛而谈"的根源。
GUIDE_SYSTEM = """你是旅行攻略分析专家。输入是若干条该城市高赞攻略视频的文案与高赞评论，
请只从这些素材里提炼可直接用于行程规划的结构化知识。

规则：
1. mentions：素材里真实提到的具体景点/餐厅/体验，按被提及频次与语气强度排序；
   每条给 name（具体到可直接搜索的名称，如"云冈石窟"而非"古建筑"）、
   category（景点|美食|体验|购物）、heat（高|中|低）、note（一句话：为何值得去或要注意什么）；
2. plan_hints：行程编排知识，每条一句话且必须可执行，例如
   "三天两夜建议住前门/王府井片区，地铁直达主要景点"、
   "故宫和景山可以连着走，出神武门就是景山北门"、
   "周一多数博物馆闭馆，第一天别排室内馆"、"XX 商业化严重，时间紧可以跳过"；
   禁止写空话（如"合理安排时间""注意安全"）；
3. itineraries：素材里出现的"逐日行程编排"（如"第一天宽窄巷子+人民公园，第二天都江堰"），
   逐条记录每条视频的编排：{"days": [{"day": 1, "slots": [{"slot": "上午|下午|晚上|全天", "spot": "名称"}]}]}；
   只记素材里明确说到的点位与顺序，视频没讲到的天不要凑；最多 3 条（挑信息最完整的）；没有就给空数组；
4. 只提炼素材里真实出现的内容，禁止补充你自己的常识；素材没提到就不要编；
   时效敏感信息（票价/开放时间/预约规则）如无法确定，在 plan_hints 里注明"待核实"；
5. days_advice：素材里提到的建议游玩天数（整数，没提到给 null）；
6. stay_advice：素材里推荐的住宿片区（字符串，没提到给空串）；
7. 输出严格 JSON：{"mentions": [{"name": "...", "category": "...", "heat": "...", "note": "..."}],
   "plan_hints": ["..."], "itineraries": [], "days_advice": null, "stay_advice": ""}"""

FOOD_SYSTEM = """你是本地美食向导。列出该城市最值得去的餐厅/特色小吃店（游客真实会去、有讨论热度的）。
规则：
1. 具体到店名或招牌小吃名（如"凤临阁""东方削面""浑源凉粉"），不要泛泛的"当地美食"；
2. 覆盖本地代表性特色（特色小吃/老字号/人气馆子），不重复；
3. 输出严格 JSON：{"foods": ["..."]}"""

VERIFY_SYSTEM = """你是信息核查专家。输入是候选清单及各自的抖音验证统计（正面/负面证据、营销号占比、评论摘录），
判断每个候选是否值得排进行程。

规则：
1. verdict=drop 的情形：完全没有有效视频；正面证据弱且负面证据强；营销号占比超过一半；
2. pitfall_risk 依据负面证据强度：低/中/高；
3. evidence 依据正面证据强度：强/中/弱；
4. reason 一句话说明判断依据（引用关键评论摘录内容）；
5. 最终 keep 的候选控制在 8~12 个：景点类至少保留 4 个（行程骨架），名额不足时优先砍购物、其次体验；
   美食类只要有正面证据且营销号不过半就应 keep（餐厅是午/晚餐推荐的刚需，名额不与景点竞争）；
6. 输出严格 JSON：{"results": [{"name": "...", "verdict": "keep|drop", "evidence": "强|中|弱",
   "pitfall_risk": "低|中|高", "reason": "..."}]}"""


def candidate_foods(city: str, max_n: int) -> list[str]:
    """圈定城市美食候选（店名/小吃名）：指定景点路径与热度刷榜用。联网能力随候选链生效。"""
    data = chat_json(FOOD_SYSTEM, f"城市：{city}\n数量上限：{max_n}", web_search=LLM_WEB_SEARCH)
    seen: set[str] = set()
    out: list[str] = []
    for x in data.get("foods") or []:
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:max_n]


def _guide_corpus(items, max_chars: int = 12000) -> str:
    """把城市攻略视频的文案与高赞评论拼成 LLM 输入（带点赞/发布时间/来源，便于溯源）。

    评论按点赞降序只取前若干条：高赞评论密度更高，且控制 token 成本。纯函数可测。"""
    blocks = []
    for i, it in enumerate(items or [], 1):
        get = (it.get if isinstance(it, dict) else lambda k, d=None: getattr(it, k, d))
        head = (f"[视频{i}] 点赞 {get('like_count') or 0}"
                f"｜收藏 {get('collect_count') or 0}"
                f"｜发布 {get('publish_time') or '未知'}｜{get('url') or ''}")
        lines = [head]
        desc = str(get("description") or "").strip()
        if desc:
            lines.append("文案：" + desc[:600])
        cs = sorted((get("comments") or []), key=lambda c: (getattr(c, "like_count", 0) or 0),
                    reverse=True)[:GUIDE_COMMENTS_PER_VIDEO]
        for c in cs:
            lines.append(f"  评论({getattr(c, 'like_count', 0) or 0}赞)："
                         f"{str(getattr(c, 'text', '') or '')[:200]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:max_chars]


def _normalize_mention(x) -> dict | None:
    """攻略提及项归一：名称必填，类别/热度越界回默认值（防 LLM 自由发挥）。纯函数可测。"""
    if not isinstance(x, dict):
        return None
    name = str(x.get("name") or "").strip()
    if not name:
        return None
    cat = str(x.get("category") or "").strip()
    heat = str(x.get("heat") or "").strip()
    return {"name": name,
            "category": cat if cat in CATEGORY_QUOTA else "景点",
            "heat": heat if heat in ("高", "中", "低") else "中",
            "note": str(x.get("note") or "").strip()}


# 草案时段枚举（与 planner.ALLOWED_SLOTS 同口径；"" 表示素材没写时段，仅作点位记录）
ITINERARY_SLOTS = ("上午", "下午", "晚上", "全天")


def _normalize_itinerary(x) -> dict | None:
    """单条视频行程草案归一：逐日逐时段点位，名称缺失或天数非法的项直接丢弃。

    纯函数可测；归一后无有效天则返回 None（不拿半截草案去污染审核输入）。"""
    if not isinstance(x, dict):
        return None
    days_out: list[dict] = []
    for d in x.get("days") or []:
        if not isinstance(d, dict):
            continue
        try:
            day_no = int(d.get("day"))
        except (TypeError, ValueError):
            continue
        if day_no < 1:
            continue
        slots: list[dict] = []
        for it in d.get("slots") or d.get("items") or []:
            if not isinstance(it, dict):
                continue
            spot = str(it.get("spot") or it.get("name") or "").strip()
            if not spot:
                continue
            slot = str(it.get("slot") or it.get("period") or "").strip()
            slots.append({"slot": slot if slot in ITINERARY_SLOTS else "", "spot": spot})
        if slots:
            days_out.append({"day": day_no, "slots": slots})
    return {"days": days_out} if days_out else None


def draft_spot_names(draft: dict | None) -> list[str]:
    """草案里的点位名（去重保序）：验证优先级与候选注入共用。纯函数可测。"""
    names: list[str] = []
    for d in (draft or {}).get("days") or []:
        if not isinstance(d, dict):
            continue
        for it in d.get("slots") or []:
            if isinstance(it, dict):
                s = str(it.get("spot") or "").strip()
                if s:
                    names.append(s)
    return list(dict.fromkeys(names))


def empty_guide_knowledge() -> dict:
    """攻略层空骨架（未启用/无素材/提炼失败时返回，调用方据此降级为纯 LLM 圈定）。"""
    return {"guide_candidates": [], "plan_hints": [], "guide_itineraries": [],
            "days_advice": None, "stay_advice": "", "sources": [], "videos": 0}


def extract_guide_knowledge(items, city: str = "", days: int = 0) -> dict:
    """从城市高赞攻略视频提炼"真实候选 + 编排知识 + 视频行程草案"（M6-B）。只调 LLM，不做采集。

    返回 {guide_candidates, plan_hints, guide_itineraries, days_advice, stay_advice, sources, videos}。
    素材为空或提炼失败一律返回空骨架（绝不抛错阻断主流程，也绝不编造内容）。"""
    corpus = _guide_corpus(items)
    if not corpus.strip():
        return empty_guide_knowledge()
    try:
        data = chat_json(GUIDE_SYSTEM,
                         f"城市：{city or '未指定'}\n用户天数：{days or '未指定'}\n\n"
                         f"攻略素材：\n{corpus}")
    except Exception:
        return empty_guide_knowledge()
    seen: set[str] = set()
    mentions: list[dict] = []
    for x in data.get("mentions") or []:
        m = _normalize_mention(x)
        if m and m["name"] not in seen:
            seen.add(m["name"])
            mentions.append(m)
    hints = [str(h).strip() for h in (data.get("plan_hints") or []) if str(h).strip()]
    drafts = [d for d in (_normalize_itinerary(x) for x in (data.get("itineraries") or [])) if d]
    try:
        days_advice = int(data.get("days_advice")) if data.get("days_advice") else None
    except (TypeError, ValueError):
        days_advice = None
    return {
        "guide_candidates": mentions[:GUIDE_MENTION_MAX],
        "plan_hints": list(dict.fromkeys(hints))[:GUIDE_HINTS_MAX],
        "guide_itineraries": drafts[:GUIDE_ITINERARY_MAX],
        # 天数建议超出合法区间（1~7）视为无效，不拿它去覆盖用户输入
        "days_advice": days_advice if days_advice and 1 <= days_advice <= 7 else None,
        "stay_advice": str(data.get("stay_advice") or "").strip(),
        "sources": [str(getattr(it, "url", "") or (it.get("url") if isinstance(it, dict) else ""))
                    for it in (items or [])][:10],
        "videos": len(items or []),
    }


# 视频行程草案审核（M6-C）：先把高赞攻略视频里怎么排的读进来，审核增删改后作为规划主干，
# 再把草案点位逐个丢给验证采集——"先看别人怎么玩，再逐个查证"，用户踩过的坑不重踩。
REVIEW_SYSTEM = """你是行程草案审核专家。输入是若干条高赞攻略视频里提炼的"逐日行程编排"（可能互不一致）
与一份候选点池，请合并审核成一份供最终规划使用的行程草案。

规则：
1. 主干取"被多条视频反复提到的编排"（共识度高、是真实被走过的路线），只出现一次的编排可留但优先级低；
2. 天数对齐：草案天数多于用户天数时砍可跳过项（保留标志性景点）；少于用户天数时从候选点池挑点补足
   （优先攻略热度高、与相邻点位顺路的），禁止使用点池以外的点；
3. 每天排 2~3 个时段（上午/下午/晚上）；古镇/主题乐园/山岳这类需整天的点用"全天"单独占一天；
   素材不足时如实少排点位，禁止硬凑——但不要产出"某天只有一个时段有安排"的草稿；
4. 点位名称必须原样照抄输入的草案或点池，禁止缩写/改写/翻译（改了就检索不到，后续验证会失败）；
5. 明显不合理的编排要修正（顺序绕路、闭馆日冲突、一天塞太多点），并在 notes 里说明；
6. 输出严格 JSON：{"days": [{"day": 1, "slots": [{"slot": "上午|下午|晚上|全天", "spot": "..."}]}],
   "notes": "一句话说明主要修改（补了什么、砍了什么、为什么）"}"""


def review_guide_itinerary(city: str, days: int, preferences: str,
                           guide: dict | None) -> dict:
    """审核"视频行程草案"（M6-C）：合并多条视频的逐日编排 -> 对齐用户天数 -> 增删改点位。

    返回 {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "..."}]}], "notes": "..."}；
    无草案素材或审核失败返回 {}（调用方据此跳过注草案，零回归）。只调 LLM，不做采集。"""
    g = guide or {}
    lines: list[str] = []
    drafts = [d for d in (g.get("guide_itineraries") or []) if isinstance(d, dict)]
    for i, d in enumerate(drafts, 1):
        bits = []
        for dd in d.get("days") or []:
            if not isinstance(dd, dict):
                continue
            seg = "；".join(f"{s.get('slot') or ''} {s.get('spot')}".strip()
                           for s in (dd.get("slots") or [])
                           if isinstance(s, dict) and s.get("spot"))
            if seg:
                bits.append(f"第{dd.get('day')}天：{seg}")
        if bits:
            lines.append(f"[视频行程{i}] " + "｜".join(bits))
    if not lines:
        return {}
    pool = list(dict.fromkeys(
        str(m.get("name") or "").strip() for m in (g.get("guide_candidates") or [])
        if isinstance(m, dict) and str(m.get("name") or "").strip()))
    user = (f"城市：{city}\n用户天数：{days} 天\n用户偏好：{preferences or '无'}\n\n"
            "视频行程草案：\n" + "\n".join(lines))
    if pool:
        user += "\n\n候选点池（可挑点补足天数）：" + "、".join(pool[:GUIDE_MENTION_MAX])
    try:
        data = chat_json(REVIEW_SYSTEM, user)
    except Exception:
        return {}
    out_days: list[dict] = []
    for d in data.get("days") or []:
        norm = _normalize_itinerary({"days": [d]})
        if norm:
            out_days.extend(norm["days"])
    if days:
        out_days = [d for d in out_days if d["day"] <= days]
    if not out_days:
        return {}
    return {"days": out_days, "notes": str(data.get("notes") or "").strip()}


def _normalize_candidate(x: dict) -> dict | None:
    name = str(x.get("name") or "").strip()
    category = str(x.get("category") or "").strip()
    if not name:
        return None
    if category not in CATEGORY_QUOTA:
        category = "景点"
    return {"name": name, "category": category, "reason": str(x.get("reason") or "").strip()}


def generate_candidates(city: str, days: int, preferences: str, *,
                        guide_evidence: dict | None = None,
                        draft_plan: dict | None = None) -> list[dict]:
    """大模型圈定候选：按类别配额裁剪后返回（上限 TOTAL_CANDIDATES_MAX）。

    guide_evidence（M6-B）：城市高赞攻略视频的真实提炼结果。传入时候选以"攻略里
    真实被反复提到的点"为底稿、LLM 只做补充与配额平衡，取代从前凭空想象；
    draft_plan（M6-C）：审核后的视频行程草案。草案点位确定性前置进候选清单且不受
    类别配额限制（行程主干，必须逐个进验证采集），LLM 补充点仍受配额约束；
    两者不传则保持原纯 LLM 行为（kernel-only / 攻略层未启用时零回归）。
    联网搜索随配置生效（服务商不支持时自动降级为纯基线）。"""
    user = (f"城市：{city}\n天数：{days}\n用户偏好：{preferences or '无'}\n"
            f"类别配额：{CATEGORY_QUOTA}")
    gc = (guide_evidence or {}).get("guide_candidates") or []
    if gc:
        user += ("\n\n以下是该城市高赞攻略视频里真实被提到的点（已经过实地采集验证，"
                 "比你的常识更贴近当下；优先纳入，尤其热度高的不要遗漏）：\n"
                 + "\n".join(f"- {m['name']}（{m['category']}｜攻略提及热度{m['heat']}"
                             + (f"｜{m['note']}" if m.get("note") else "") + "）"
                             for m in gc))
        hints = (guide_evidence or {}).get("plan_hints") or []
        if hints:
            user += ("\n\n攻略里的编排建议（据此判断哪些点适合本次天数、哪些可以舍）：\n"
                     + "\n".join(f"- {h}" for h in hints[:8]))
        stay = str((guide_evidence or {}).get("stay_advice") or "").strip()
        if stay:
            user += f"\n\n攻略推荐的住宿片区：{stay}"
    draft_names = draft_spot_names(draft_plan)
    if draft_names:
        user += ("\n\n以下是高赞攻略视频行程草案里已确定的点位，必须全部纳入候选清单"
                 "（本次行程主干，逐个验证即可）：\n- " + "\n- ".join(draft_names))
    data = chat_json(GEN_SYSTEM, user, web_search=LLM_WEB_SEARCH)
    raw = [c for c in (_normalize_candidate(x)
                       for x in (data.get("candidates") or []) if isinstance(x, dict)) if c]
    # 草案点位前置：类别取自攻略提及或 LLM 候选，都查不到就按景点处理（骨架优先）
    cat_map = {str(m.get("name") or "").strip(): str(m.get("category") or "")
               for m in gc if isinstance(m, dict)}
    cat_map.update({c["name"]: c["category"] for c in raw})
    draft_set = set(draft_names)
    pri = [_normalize_candidate({"name": n, "category": cat_map.get(n) or "景点",
                                 "reason": "视频行程草案点位（攻略视频里真实被走过的编排）"})
           for n in draft_names]
    picked: list[dict] = []
    seen_names: set[str] = set()
    per_cat: dict[str, int] = {c: 0 for c in CATEGORY_QUOTA}
    for c in [x for x in pri + raw if x]:
        if c["name"] in seen_names:
            continue
        if c["name"] not in draft_set and per_cat[c["category"]] >= CATEGORY_QUOTA[c["category"]]:
            continue
        seen_names.add(c["name"])
        per_cat[c["category"]] += 1
        picked.append(c)
        if len(picked) >= TOTAL_CANDIDATES_MAX:
            break
    return picked


def select_verify_candidates(cands: list[dict], verify_max: int = VERIFY_MAX,
                             priority: list[str] | None = None) -> list[dict]:
    """按类别配额公平挑选进入验证采集的候选，取代"按返回顺序硬截断 [:verify_max]"。

    priority（M6-C）：视频行程草案点位，先占验证名额（行程主干必须逐个搜验证），
    剩余名额再按类别配额公平分配；不传则保持原行为。
    先按 VERIFY_QUOTA 给每类保底名额（取 min(配额, 实际数)），名额有剩再把余量
    按"景点→美食→体验→购物"优先补给还有候选的类别，直到用满 verify_max。
    类别内保持 LLM 原始顺序（靠前通常更值得验证）。纯函数，独立可测。
    """
    pri_order = {n: i for i, n in enumerate(priority or [])}
    pri = sorted((c for c in cands if c.get("name") in pri_order),
                 key=lambda c: pri_order[c["name"]])[:verify_max]
    taken = {c["name"] for c in pri}
    room = verify_max - len(pri)
    by_cat: dict[str, list[dict]] = {}
    for c in cands:
        if c.get("name") not in taken:
            by_cat.setdefault(c.get("category") or "景点", []).append(c)
    picked: list[dict] = []
    remaining: dict[str, list[dict]] = {}
    for cat, quota in VERIFY_QUOTA.items():
        avail = by_cat.get(cat, [])
        picked.extend(avail[:quota])
        if avail[quota:]:
            remaining[cat] = avail[quota:]
    for cat, avail in by_cat.items():  # 未知类别整体进剩余池
        if cat not in VERIFY_QUOTA and avail:
            remaining[cat] = avail
    if len(picked) < room:  # 名额有剩：景点优先补，再按配额顺序补
        for cat in ["景点", "美食", "体验", "购物", *list(remaining)]:
            while len(picked) < room and remaining.get(cat):
                picked.append(remaining[cat].pop(0))
    return (pri + picked)[:verify_max]


def _normalize_result(x: dict, known: set[str]) -> dict | None:
    name = str(x.get("name") or "").strip()
    if not name or name not in known:
        return None
    verdict = str(x.get("verdict") or "").strip().lower()
    evidence = str(x.get("evidence") or "").strip()
    risk = str(x.get("pitfall_risk") or "").strip()
    return {
        "name": name,
        "verdict": "keep" if verdict == "keep" else "drop",
        "evidence": evidence if evidence in ("强", "中", "弱") else "弱",
        "pitfall_risk": risk if risk in ("低", "中", "高") else "中",
        "reason": str(x.get("reason") or "").strip(),
    }


def verify_candidates(candidates: list[dict], stats: dict[str, dict]) -> list[dict]:
    """交叉验证筛选：输入候选 + 验证统计，输出带 verdict 的评审结果。

    stats 形如 {name: {"videos", "marketing_hits", "positive", "negative", "sample_quotes"}}。
    兜底：LLM 未覆盖或有缺陷的候选按保守规则补判（有正面证据则 keep）。
    """
    known = {c["name"] for c in candidates}
    lines = []
    for c in candidates:
        s = stats.get(c["name"]) or {}
        ratio = (s.get("marketing_hits") or 0) / max(1, s.get("videos") or 1)
        lines.append(
            f"- {c['name']}（{c['category']}）：视频 {s.get('videos', 0)} 条，"
            f"营销号占比 {ratio:.0%}，正面证据 {s.get('positive', 0)} 条，"
            f"负面证据 {s.get('negative', 0)} 条；评论摘录：{'；'.join((s.get('sample_quotes') or [])[:3]) or '无'}"
        )
    data = chat_json(VERIFY_SYSTEM, "候选验证统计：\n" + "\n".join(lines))
    results = {
        r["name"]: r
        for r in (
            _normalize_result(x, known) for x in (data.get("results") or []) if isinstance(x, dict)
        )
        if r
    }
    # 保守兜底：评审遗漏的候选，有正面证据且营销号不过半则 keep
    for c in candidates:
        if c["name"] in results:
            continue
        s = stats.get(c["name"]) or {}
        ratio = (s.get("marketing_hits") or 0) / max(1, s.get("videos") or 1)
        ok = (s.get("positive") or 0) > 0 and ratio <= 0.5
        results[c["name"]] = {
            "name": c["name"],
            "verdict": "keep" if ok else "drop",
            "evidence": "中" if ok else "弱",
            "pitfall_risk": "中",
            "reason": "自动兜底判定（评审未覆盖）",
        }
    # 数量控制：keep 过多时按证据强度裁到 KEEP_MAX（景点保底不裁）
    cat_of = {c["name"]: c["category"] for c in candidates}
    keeps = [r for r in results.values() if r["verdict"] == "keep"]
    if len(keeps) > KEEP_MAX:
        order = {"强": 0, "中": 1, "弱": 2}
        keeps.sort(key=lambda r: (order.get(r["evidence"], 3), 0 if cat_of[r["name"]] == "景点" else 1))
        cut = {r["name"] for r in keeps[KEEP_MAX:] if cat_of[r["name"]] != "景点"}
        for r in results.values():
            if r["name"] in cut:
                r["verdict"] = "drop"
                r["reason"] += "（超出保留上限被裁剪）"
    # 美食保底：餐厅全被评审淘汰但有正面证据时，恢复证据最强的前 2 家
    # （午/晚餐推荐是行程刚需，全灭会让 LLM 转而编造餐厅）；因超额被裁的不恢复
    foods = [c for c in candidates if c["category"] == "美食"]
    if foods and not any(results[c["name"]]["verdict"] == "keep" for c in foods):
        order = {"强": 0, "中": 1, "弱": 2}
        revivable = []
        for c in foods:
            r = results[c["name"]]
            s = stats.get(c["name"]) or {}
            ratio = (s.get("marketing_hits") or 0) / max(1, s.get("videos") or 1)
            if (s.get("positive") or 0) > 0 and ratio <= 0.5 and "被裁剪" not in r["reason"]:
                revivable.append((order.get(r["evidence"], 3), -(s.get("positive") or 0), r))
        revivable.sort(key=lambda x: x[:2])
        for _, _, r in revivable[:2]:
            r["verdict"] = "keep"
            r["reason"] += "（美食保底恢复：有正面证据且营销号不过半）"
    return [results[c["name"]] for c in candidates if c["name"] in results]
