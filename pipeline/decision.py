"""统一 POI 决策对象（PRD §4.2 / §5.1，M1 地基）。

一个地点从"被基线提名"到"最终落稿/落选"，全程携带同一组证据属性，
任何阶段不得把证据压扁成布尔值或一段自由文本后丢弃结构化字段。

**双写过渡约定（PRD §4.2 迁移策略）**：本模块只做"从现有平行结构组装统一对象"，
不改动任何既有函数的签名与返回结构。质量门禁（pipeline/qc.py）与选点决策表只读
本模块产物，其余渲染路径继续读旧结构；逐模块切换完成后再删旧拼装代码。

防御性原则：所有入参都可为 None 或缺项，缺什么就用安全默认值（空串/0/None），
绝不抛异常中断主流程；数据缺失显式标注"待核实"，禁止臆造数字。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from pipeline.planner import pick_ticket_price   # 门票唯一口径（详情卡与行程槽位共用，planner 不反向依赖）

# —— 决策状态（对外展示的三态）——
STATE_IN = "入选"
STATE_ALT = "备选"
STATE_OUT = "淘汰"
DECISION_STATES = (STATE_IN, STATE_ALT, STATE_OUT)

# —— R2 硬理由闭集（PRD F5.1）——
# 高价值点未入选时，decision.reason 必须命中其一才算"有硬理由"；
# 自由文本理由不通过，防止规划器随便给句话就绕过门禁。
HARD_REASONS = (
    "闭馆日冲突",
    "单日时长不足",
    "用户明确排除",
    "需整天而剩余天数不够",
)

# —— 证据强度：对外只暴露这一套（PRD §5.1 映射约定）——
EVIDENCE_STRONG, EVIDENCE_MID, EVIDENCE_WEAK = "强", "中", "弱"
EVIDENCE_LEVELS = (EVIDENCE_STRONG, EVIDENCE_MID, EVIDENCE_WEAK)

# 置信度分级（pipeline/verify.py 产出的内部字段）到证据强度的映射用词
_CONF_HIGH, _CONF_MID = "高置信度", "中置信度"


def evidence_from_conf(conf_level: str | None, n_sources: int | None) -> str:
    """把既有置信度分级映射成对外证据强度。纯函数可测。

    映射规则（PRD §5.1）：强 = 高置信度且独立来源 ≥3；中 = 中置信度；
    弱 = 低置信度（含单源与营销号来源）。信息缺失时保守给"弱"。"""
    n = int(n_sources or 0)
    if conf_level == _CONF_HIGH and n >= 3:
        return EVIDENCE_STRONG
    if conf_level in (_CONF_HIGH, _CONF_MID):
        return EVIDENCE_MID
    return EVIDENCE_WEAK


def spot_evidence(verify_evidence: str | None, points: list[dict] | None) -> str:
    """地点级证据强度：优先用交叉验证结论，缺失时由要点的置信度分布推导。

    推导口径：≥2 条高置信度要点 = 强；1 条高置信度或 ≥2 条中置信度 = 中；
    其余（含无要点）= 弱。纯函数可测。"""
    if verify_evidence in EVIDENCE_LEVELS:
        return verify_evidence
    pts = [p for p in (points or []) if isinstance(p, dict)]
    n_high = sum(1 for p in pts if p.get("conf_level") == _CONF_HIGH)
    n_mid = sum(1 for p in pts if p.get("conf_level") == _CONF_MID)
    if n_high >= 2:
        return EVIDENCE_STRONG
    if n_high >= 1 or n_mid >= 2:
        return EVIDENCE_MID
    return EVIDENCE_WEAK


@dataclass
class VerifyInfo:
    """交叉验证结论（F1.1：淘汰点也必须保留，不得只留名称）。"""

    verdict: str = ""            # keep / drop / ""（未进入验证，如候选被截断）
    evidence: str = EVIDENCE_WEAK
    pitfall_risk: str = "中"     # 低 / 中 / 高
    reason: str = ""             # 一句话验证或淘汰理由，必须保留到最终报告

    @property
    def verified(self) -> bool:
        return self.verdict in ("keep", "drop")


@dataclass
class HeatInfo:
    """热度与口碑画像（F1.2：必须在规划之前算好并喂给规划器）。"""

    score: float = 0.0
    trend: str = ""
    videos: int = 0
    likes: int = 0
    comments: int = 0
    fresh_ratio: float = 0.0
    mkt_ratio: float = 0.0       # 营销号占比，≥0.5 需预警
    sentiment: str = ""          # 评论情感趋势四态

    @property
    def measured(self) -> bool:
        return self.videos > 0


@dataclass
class OfficialFact:
    """官方事实（PRD F2.2，M2 落地）：无则全空，不得编造。

    来源三层降级：seed_yaml（仓库内种子事实）> amap（高德 POI）> 空（标待核实）。"""

    price: float | None = None
    price_note: str = ""
    open_hours: str = ""
    close_day: str = ""
    booking_rule: str = ""
    event_start: str = ""
    event_end: str = ""
    valid_until: str = ""
    source_url: str = ""
    fetched_at: str = ""
    source: str = ""

    @property
    def missing(self) -> bool:
        """完全无官方数据：渲染层据此标"待核实"，门票价宁缺不编。"""
        return not (self.price is not None or self.open_hours
                    or self.close_day or self.booking_rule)


@dataclass
class DecisionInfo:
    """最终决策（由规划 + 门禁共同确定）。"""

    state: str = ""              # 入选 / 备选 / 淘汰
    day: int | None = None
    slot: str = ""               # 上午 / 下午 / 晚上 / 全天
    order: int | None = None     # 当日序号
    reason: str = ""             # 可展示理由

    @property
    def has_hard_reason(self) -> bool:
        """理由是否命中 R2 硬理由闭集（自由文本不算）。"""
        return any(r in (self.reason or "") for r in HARD_REASONS)


@dataclass
class SpotDecision:
    """单个地点的全生命周期对象（PRD §5.1 字段契约）。"""

    name: str
    category: str = "景点"       # 景点 / 美食 / 体验 / 购物
    source_tags: list[str] = field(default_factory=list)   # baseline / ugc / official
    candidate_reason: str = ""
    verify: VerifyInfo = field(default_factory=VerifyInfo)
    heat: HeatInfo = field(default_factory=HeatInfo)
    official: OfficialFact = field(default_factory=OfficialFact)
    profile: dict = field(default_factory=dict)            # 沿用 build_spot_profile 产物结构
    points: list[dict] = field(default_factory=list)       # 要点（含 conf_level/n_sources/quote）
    sources: list[str] = field(default_factory=list)       # 溯源链接
    location: str = ""                                     # "经度,纬度"
    travel_legs: list[str] = field(default_factory=list)   # 与本点相关的通行方案
    decision: DecisionInfo = field(default_factory=DecisionInfo)

    @property
    def is_food(self) -> bool:
        return self.category == "美食"

    @property
    def researched(self) -> bool:
        """是否真的调研过（有要点或有溯源链接）；未调研的点不得参与排程。"""
        return bool(self.points or self.sources)

    def to_row(self) -> dict:
        """选点决策表的一行（F7.1 渲染用，也是离线测试的断言入口）。"""
        return {
            "name": self.name,
            "category": self.category,
            "sources": "、".join(self.source_tags) or "—",
            "evidence": self.verify.evidence,
            "heat_score": self.heat.score,
            "heat_trend": self.heat.trend or "—",
            "mkt_ratio": self.heat.mkt_ratio,
            "state": self.decision.state or "未定",
            "reason": self.decision.reason or self.verify.reason or "—",
            "day": self.decision.day,
            "slot": self.decision.slot,
        }


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_official(raw: dict | None) -> OfficialFact:
    """把外部官方事实（种子 YAML / 高德 POI）规范化成 OfficialFact。纯函数可测。

    非法或缺失字段一律留空；价格无法解析时保持 None（渲染层标"待核实"），
    绝不退化成 0——0 会被下游当成"免费"。"""
    raw = raw if isinstance(raw, dict) else {}
    price = raw.get("price")
    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    return OfficialFact(
        price=price,
        price_note=str(raw.get("price_note") or "").strip(),
        open_hours=str(raw.get("open_hours") or "").strip(),
        close_day=str(raw.get("close_day") or "").strip(),
        booking_rule=str(raw.get("booking_rule") or "").strip(),
        event_start=str(raw.get("event_start") or "").strip(),
        event_end=str(raw.get("event_end") or "").strip(),
        valid_until=str(raw.get("valid_until") or "").strip(),
        source_url=str(raw.get("source_url") or "").strip(),
        fetched_at=str(raw.get("fetched_at") or "").strip(),
        source=str(raw.get("source") or "").strip(),
    )


def official_expired(fact: OfficialFact, today: str) -> bool:
    """官方事实是否已过有效期（PRD F6.1）。纯函数可测。

    无 valid_until 视为未过期（宁可继续用并提示核实，也不静默丢弃）；
    日期字符串按 ISO 前 10 位比较，格式非法时保守判未过期。"""
    if not fact.valid_until or not today:
        return False
    return str(fact.valid_until)[:10] < str(today)[:10]


def build_decisions(*, candidates: list[dict] | None = None,
                    verify_results: list[dict] | None = None,
                    profiles: dict[str, dict] | None = None,
                    food_profiles: dict[str, dict] | None = None,
                    points_by_spot: dict[str, list[dict]] | None = None,
                    sources_by_spot: dict[str, list[str]] | None = None,
                    heat_rows: list[dict] | None = None,
                    official_facts: dict[str, dict] | None = None,
                    locs: dict[str, str] | None = None,
                    travel_lines: list[str] | None = None,
                    plan: dict | None = None) -> list[SpotDecision]:
    """从现有平行结构组装统一决策对象（双写过渡期的唯一适配层）。纯函数可测。

    以 name 为主键做并集：候选清单（含被淘汰、被截断的）∪ 已调研档案 ∪ 热度行 ∪
    规划排入的点，保证"每个被圈定过的候选都查得到结论"（F1.1 的 AC）。
    返回顺序：入选（按天/时段）→ 备选 → 淘汰 → 未定，便于决策表直接渲染。"""
    by_name: dict[str, SpotDecision] = {}

    def ensure(name: str, category: str = "景点") -> SpotDecision:
        key = str(name or "").strip()
        if not key:
            raise ValueError("地点名称为空")
        if key not in by_name:
            by_name[key] = SpotDecision(name=key, category=category or "景点")
        elif category and by_name[key].category == "景点" and category != "景点":
            by_name[key].category = category    # 美食等更具体的类别覆盖默认值
        return by_name[key]

    # 1) 候选清单：基线提名理由与类别（含未进入验证的，F2.1 截断公平性依赖它可见）
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        d = ensure(c.get("name"), str(c.get("category") or "景点"))
        d.candidate_reason = str(c.get("reason") or "").strip()
        if "baseline" not in d.source_tags:
            d.source_tags.append("baseline")

    # 2) 交叉验证结论：verdict/evidence/pitfall_risk/reason 全量保留（淘汰点也留）
    for r in verify_results or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        d = ensure(r["name"])
        d.verify = VerifyInfo(
            verdict=str(r.get("verdict") or "").strip(),
            evidence=str(r.get("evidence") or "").strip() or EVIDENCE_WEAK,
            pitfall_risk=str(r.get("pitfall_risk") or "中").strip(),
            reason=str(r.get("reason") or "").strip(),
        )
        if d.verify.evidence not in EVIDENCE_LEVELS:
            d.verify.evidence = EVIDENCE_WEAK

    # 3) 调研产物：档案 + 要点 + 溯源（景点与餐厅同等待遇）
    for table, cat in ((profiles or {}, "景点"), (food_profiles or {}, "美食")):
        for name, p in table.items():
            d = ensure(name, cat)
            d.profile = p if isinstance(p, dict) else {}
            if "ugc" not in d.source_tags:
                d.source_tags.append("ugc")
    for name, pts in (points_by_spot or {}).items():
        d = ensure(name)
        d.points = [p for p in (pts or []) if isinstance(p, dict)]
        if d.points and "ugc" not in d.source_tags:
            d.source_tags.append("ugc")
    for name, urls in (sources_by_spot or {}).items():
        d = ensure(name)
        d.sources = [str(u) for u in (urls or []) if u]

    # 4) 热度与口碑：F1.2 要求在规划之前就绪
    for h in heat_rows or []:
        if not isinstance(h, dict) or not h.get("spot"):
            continue
        d = ensure(h["spot"])
        d.heat = HeatInfo(
            score=round(_num(h.get("score")), 3),
            trend=str(h.get("trend") or "").strip(),
            videos=int(_num(h.get("videos"))),
            likes=int(_num(h.get("likes"))),
            comments=int(_num(h.get("comments"))),
            fresh_ratio=round(_num(h.get("fresh_ratio")), 2),
            mkt_ratio=round(_num(h.get("mkt_ratio")), 2),
            sentiment=str(h.get("sentiment") or "").strip(),
        )

    # 5) 官方事实（M2 落地；当前无数据则全空并标待核实）
    for name, raw in (official_facts or {}).items():
        d = ensure(name)
        d.official = normalize_official(raw)
        if not d.official.missing and "official" not in d.source_tags:
            d.source_tags.append("official")

    # 6) 空间信息
    for name, loc in (locs or {}).items():
        if name in by_name and loc:
            by_name[name].location = str(loc)
    for line in travel_lines or []:
        text = str(line or "")
        for d in by_name.values():
            if d.name and d.name in text and text not in d.travel_legs:
                d.travel_legs.append(text)

    # 7) 证据强度补齐：验证阶段没给结论的（如被截断未验证），按要点置信度推导
    for d in by_name.values():
        if d.verify.evidence == EVIDENCE_WEAK and d.verify.verdict != "drop":
            d.verify.evidence = spot_evidence(None, d.points)

    decisions = list(by_name.values())
    apply_plan(decisions, plan or {})
    return sort_decisions(decisions)


def apply_plan(decisions: list[SpotDecision], plan: dict) -> None:
    """按规划结果就地标注决策状态与排程位置（PRD §5.1 decision 字段组）。

    判定顺序：
    1. 排进 slots → 入选，写 day/slot/order；
    2. 未排入但 summary_note 点名列为备选 → 备选，理由取 summary_note；
    3. 未排入且交叉验证 verdict=drop → 淘汰，理由取 verify.reason；
    4. 其余未排入 → 备选并标"未排入且规划未说明原因"（交门禁 R2 判是否静默丢弃）。
    """
    note = str((plan or {}).get("summary_note") or "")
    planned_names: set[str] = set()
    order_in_day: dict[int, int] = {}
    for day in (plan or {}).get("days") or []:
        if not isinstance(day, dict):
            continue
        d_no = int(_num(day.get("day"), 0)) or None
        for slot in day.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            name = str(slot.get("spot") or "").strip()
            if not name:
                continue
            planned_names.add(name)
            order_in_day[d_no] = order_in_day.get(d_no, 0) + 1
            for d in decisions:
                if d.name == name:
                    d.decision = DecisionInfo(
                        state=STATE_IN, day=d_no,
                        slot=str(slot.get("slot") or "").strip(),
                        order=order_in_day.get(d_no),
                        reason=str(slot.get("reasons") or "").strip() or "已排入行程",
                    )
    # 餐食推荐识别：food 字段点名推荐的餐厅也算"已用"，标入选——否则会误显示为备选、
    # 并让 R2 把强证据餐厅当成"静默丢弃"误报（餐厅覆盖由 R7 负责，不属行程骨架）
    for day in (plan or {}).get("days") or []:
        if not isinstance(day, dict):
            continue
        d_no = int(_num(day.get("day"), 0)) or None
        for slot in day.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            ftext = str(slot.get("food") or "")
            if not ftext:
                continue
            for d in decisions:
                if d.is_food and d.name and d.name in ftext and d.decision.state != STATE_IN:
                    planned_names.add(d.name)
                    d.decision = DecisionInfo(
                        state=STATE_IN, day=d_no,
                        slot=str(slot.get("slot") or "").strip(),
                        reason=f"作为{str(slot.get('slot') or '').strip()}餐食推荐")
    for d in decisions:
        if d.name in planned_names:
            continue
        if d.name and d.name in note:
            d.decision = DecisionInfo(state=STATE_ALT, reason=note.strip()[:120])
        elif d.verify.verdict == "drop":
            d.decision = DecisionInfo(
                state=STATE_OUT,
                reason=d.verify.reason or "交叉验证未通过（无有效正面证据）")
        elif d.is_food:
            # 餐饮解耦（PRD §9.3）：餐厅不排入行程时间线属预期，不当作景点式“静默丢弃”
            d.decision = DecisionInfo(
                state=STATE_ALT, reason="餐饮不排入行程时间线（见美食推荐榜），属解耦设计")
        elif d.researched:
            d.decision = DecisionInfo(state=STATE_ALT, reason="未排入且规划未说明原因")
        else:
            # 没调研过（被截断或采集失败）：既不算入选也不该冒充淘汰，标未定并说明
            d.decision = DecisionInfo(
                state=STATE_OUT if d.verify.verdict == "drop" else "",
                reason="未进入调研（候选截断或采集失败）")


def sort_decisions(decisions: list[SpotDecision]) -> list[SpotDecision]:
    """决策表展示顺序：入选（天/时段/序号）→ 备选（热度降序）→ 淘汰 → 未定。"""
    state_order = {STATE_IN: 0, STATE_ALT: 1, STATE_OUT: 2, "": 3}
    return sorted(
        decisions,
        key=lambda d: (
            state_order.get(d.decision.state, 3),
            d.decision.day if d.decision.day is not None else 99,
            d.decision.order if d.decision.order is not None else 99,
            -d.heat.score,
            d.name,
        ),
    )


# ==================== 来源性质标签（F-D4 口径说明，M1a）====================
# 预算契约（BudgetLine/BudgetPlan/R6 恒等）已退役：报告不再输出金额估算。
# 下面三个标签仍被详情卡使用（票价/人均价的“官方确定/UGC参考/待核实”性质标注）。
NATURE_OFFICIAL, NATURE_UGC, NATURE_TODO = "官方确定", "UGC参考", "待核实"


# ==================== F-A3 主体归属校验（PRD §8 Epic A，M1a）====================

def find_misattributed(texts: list[str], subject: str, other_names) -> tuple[list[str], list[str]]:
    """挑出「描述了别处、却挂在 subject 档案里」的文本（串档）。（F-A3 硬要求）

    规则：一条文本提到了 other_names 里某个别处主体、且未提到 subject 本身，判为可疑；
    容忍 substring 撞名（如“西湖”⊂“西湖醋鱼”）——只要同时出现 subject 就不算串档。
    返回 (保留列表, 可疑剔除列表)，均为原字符串引用。纯函数可测。"""
    subject = (subject or "").strip()
    others = [str(n).strip() for n in (other_names or []) if str(n).strip() and str(n).strip() != subject]
    keep: list[str] = []
    suspect: list[str] = []
    if not subject or not others:
        return list(texts or []), []
    for t in texts or []:
        s = str(t or "")
        hit_other = next((o for o in others if o in s), None)
        if hit_other is not None and subject not in s:
            suspect.append(s)
        else:
            keep.append(s)
    return keep, suspect


def apply_attribution_check(profiles: dict[str, dict]) -> dict[str, list[str]]:
    """就地过滤所有档案中描述别处的条目（F-A3）：highlights/avoid/tips 逐条校验。

    返回 {地点: [被剔除的可疑文本]}，供上层记入 removed_items / 质量报告。
    以“其他所有地点名”为别处集；宁可漏剔不误删（仅当提到别处且未提本人才剔）。"""
    names = list(profiles.keys())
    removed: dict[str, list[str]] = {}
    for name, p in profiles.items():
        if not isinstance(p, dict):
            continue
        others = [n for n in names if n != name]
        for field_ in ("highlights", "avoid", "tips"):
            keep, suspect = find_misattributed(p.get(field_, []), name, others)
            if suspect:
                p[field_] = keep
                removed.setdefault(name, []).extend(suspect)
    return removed


# ==================== M4 表达层数据契约（PRD §6.8 / §6.9 / §6.11）====================
# 决策层唯一产物 TripPlan；Markdown / HTML / API 三端只读渲染同一个对象（F-G1 生成-呈现分离）。
# 本层只做「从 SpotDecision 等已有对象投影」，绝不二次调用 LLM、绝不新造数字；
# 缺字段留空并由渲染层标「待核实」。当前以 name 作为 poi_id / food_id（无独立 id 体系）。

RANK_KIND_POI = "poi"
RANK_KIND_FOOD = "food"


@dataclass
class RankItem:
    """榜单条目（§6.8）：ref_id 必须能在 catalog 找到唯一详情（R11 无死链）。"""
    ref_id: str
    kind: str = RANK_KIND_POI            # poi / food
    rank: int = 0
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)   # {like, density, fresh}
    trend: str = ""
    state: str = ""                      # 入选 / 备选 / 淘汰（与 decision.state 联动）
    not_selected_reason: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PoiDetail:
    """地点详情档案（§6.8 / F-F3），由 SpotDecision 投影，不二次编造。"""
    poi_id: str
    name: str
    category: str = "景点"
    summary: str = ""
    ticket_price: float | None = None    # 官方价优先；无则 None（渲染标待核实），不得填 0 冒充免费
    ticket_nature: str = NATURE_TODO     # 官方确定 / UGC参考 / 待核实
    ticket_source: str = ""
    open_hours: str = ""
    close_day: str = ""
    booking_rule: str = ""
    release_time: str = ""
    best_slot: str = ""
    duration_hours: float | None = None
    is_all_day: bool = False
    heat_score: float = 0.0
    heat_breakdown: dict = field(default_factory=dict)
    trend: str = ""
    evidence: str = ""
    highlights: list[str] = field(default_factory=list)
    photo_spots: list[str] = field(default_factory=list)
    pitfalls: list[dict] = field(default_factory=list)   # {text, source}
    legs: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    state: str = ""
    plan_b: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FoodDetail:
    """美食详情档案（§6.8 / F-F2）：人均区分正餐/小吃，禁单极值当人均。"""
    food_id: str
    name: str
    area: str = ""
    avg_price: float | None = None
    avg_price_nature: str = NATURE_UGC
    price_samples_n: int = 0
    signature_dishes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    queue_risk: str = ""
    positive_density: str = ""
    pitfalls: list[dict] = field(default_factory=list)
    nearest_poi: str = ""
    evidence: str = ""
    heat_score: float = 0.0
    sources: list[str] = field(default_factory=list)
    state: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IntroSection:
    """攻略介绍小节（§6.9 / Epic E）：facts_ref 指向引用的证据对象 id，保证不另造事实（R12）。"""
    key: str
    title: str
    body: str
    facts_ref: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _first_text(d: dict, *keys) -> str:
    for k in keys:
        v = str((d or {}).get(k) or "").strip()
        if v:
            return v
    return ""


def build_poi_detail(d: SpotDecision, *, pitfall_items: list[dict] | None = None,
                     legs: list[dict] | None = None) -> PoiDetail:
    """由 SpotDecision 投影 PoiDetail。票价取官方价（无则 None+待核实，不填 0）；纯函数可测。"""
    p = d.profile if isinstance(d.profile, dict) else {}
    off = d.official
    # 官方价优先；无则用已调研门票价做 UGC 参考（catalog 与行程槽位共用同一取数口径：剔第三方代抢/套餐加价、多条取最低）；
    # 两者都无则留 None（渲染标待核实，不填 0 冒充免费），时效性交 R9
    ugc_price = pick_ticket_price(p.get("cost_items"))
    price = off.price if off.price is not None else ugc_price
    nature = (NATURE_OFFICIAL if off.price is not None
              else (NATURE_UGC if ugc_price is not None else NATURE_TODO))
    ticket_src = off.source_url or (d.sources[0] if d.sources else "")
    # 无外部 pitfall_by_spot 时回退用本点已调研 avoid（保证 catalog 详情自带避坑）
    pit_src = pitfall_items if pitfall_items is not None else [
        {"text": a, "source": ""} for a in (p.get("avoid") or [])]
    return PoiDetail(
        poi_id=d.name, name=d.name, category=d.category,
        summary=_first_text(p, "intro", "summary") or (d.points[0].get("claim", "") if d.points else ""),
        ticket_price=price, ticket_nature=nature, ticket_source=ticket_src,
        open_hours=off.open_hours or _first_text(p, "open_time"),
        close_day=off.close_day or _first_text(p, "close_day"),
        booking_rule=off.booking_rule, release_time=_first_text(p, "release_time"),
        best_slot=_first_text(p, "best_time_slot", "best_slot"),
        duration_hours=(None if p.get("duration_hours") is None else _num(p.get("duration_hours"))),
        is_all_day=bool(p.get("is_all_day")),
        heat_score=d.heat.score, trend=d.heat.trend, evidence=d.verify.evidence,
        highlights=list(p.get("highlights") or []), photo_spots=list(p.get("photo_spots") or []),
        pitfalls=[{"text": str(x.get("text") or x.get("claim") or ""), "source": str(x.get("source") or "")}
                  for x in (pit_src or []) if isinstance(x, dict) and (x.get("text") or x.get("claim"))],
        legs=list(legs or []), sources=list(d.sources),
        state=d.decision.state, plan_b="",
    )


def build_food_detail(d: SpotDecision, *, nearest_poi: str = "") -> FoodDetail:
    """由 SpotDecision（category=美食）投影 FoodDetail。人均取餐饮人均 cost_item。纯函数可测。"""
    p = d.profile if isinstance(d.profile, dict) else {}
    avg = None
    n = 0
    for c in p.get("cost_items") or []:
        if c.get("type") in ("餐饮人均", "人均") and c.get("amount"):
            avg = _num(c.get("amount"))
            n = int(_num(c.get("n") or c.get("samples") or 1))
            break
    sig = list(p.get("highlights") or [])[:4]
    return FoodDetail(
        food_id=d.name, name=d.name, area=_first_text(p, "area"),
        avg_price=avg, avg_price_nature=(NATURE_UGC if avg is not None else NATURE_TODO),
        price_samples_n=n, signature_dishes=sig,
        tags=[t for t in ([d.category] + list(p.get("tags") or [])) if t],
        queue_risk=_first_text(p, "queue_risk"), positive_density=d.verify.evidence,
        pitfalls=[{"text": a, "source": ""} for a in (p.get("avoid") or [])],
        nearest_poi=nearest_poi, evidence=d.verify.evidence, heat_score=d.heat.score,
        sources=list(d.sources), state=d.decision.state,
    )


def build_catalog(decisions: list[SpotDecision], *, legs: list[dict] | None = None,
                  pitfall_by_spot: dict[str, list[dict]] | None = None,
                  selected_order: dict[str, tuple] | None = None) -> dict:
    """由决策列表投影 catalog={poi:{id:detail}, food:{id:detail}}（F-F3）。榜单-详情一一对应的基准。"""
    poi: dict[str, dict] = {}
    food: dict[str, dict] = {}
    legs_by_spot: dict[str, list] = {}
    for lg in legs or []:
        for endpoint in (lg.get("from"), lg.get("to")):
            if endpoint and endpoint != "酒店":
                legs_by_spot.setdefault(endpoint, []).append(lg)
    food_names = [d.name for d in decisions if d.is_food]
    for d in decisions:
        if not d.name:
            continue
        if d.is_food:
            food[d.name] = build_food_detail(d, nearest_poi=_first_text(d.profile, "nearest_poi")).to_dict()
        else:
            detail = build_poi_detail(
                d, pitfall_items=(pitfall_by_spot or {}).get(d.name),
                legs=legs_by_spot.get(d.name))
            poi[d.name] = detail.to_dict()
    return {"poi": poi, "food": food, "_food_names": food_names}


def _tags_for(d: SpotDecision) -> list[str]:
    tags: list[str] = []
    if d.category and d.category != "景点":
        tags.append(d.category)
    if d.heat.mkt_ratio >= 0.5:
        tags.append("营销号偏多")
    if d.verify.evidence:
        tags.append(f"证据{d.verify.evidence}")
    return tags


def to_rank_items(decisions: list[SpotDecision], catalog: dict, *, kind: str = RANK_KIND_POI,
                  limit: int | None = None) -> list[dict]:
    """把决策投影成 RankItem 列表：按热度降序、带 ref_id/score/breakdown/state。纯函数可测。
    ref_id 与 catalog 段（poi/food）同名对齐，保证 R11 无死链。"""
    section = (catalog.get("food") if kind == RANK_KIND_FOOD else catalog.get("poi")) or {}
    pool = [d for d in decisions if d.name in section and
            ((d.is_food) if kind == RANK_KIND_FOOD else (not d.is_food))]
    pool.sort(key=lambda d: (-d.heat.score, d.name))
    if limit:
        pool = pool[:limit]
    out: list[dict] = []
    for i, d in enumerate(pool, start=1):
        not_sel = "" if d.decision.state == STATE_IN else (d.decision.reason or "")
        out.append(RankItem(
            ref_id=d.name, kind=kind, rank=i, score=round(d.heat.score, 3),
            score_breakdown={}, trend=d.heat.trend or "",
            state=d.decision.state or "", not_selected_reason=not_sel,
            tags=_tags_for(d),
        ).to_dict())
    return out


# —— F-F2 美食榜独立口径：多维可复算排序（好评/人均/热度/口碑/排队） ——

_EVIDENCE_W = {"强": 1.0, "中": 0.6, "弱": 0.3}
_QUEUE_W = {"低": 1.0, "中": 0.5, "高": 0.0}
_REPUTATION_POS = ("老字号", "本地", "老店")
_REPUTATION_NEG = ("网红", "打卡")
_FOOD_WEIGHTS = {"evidence": 0.35, "price": 0.25, "heat": 0.15,
                 "reputation": 0.15, "queue": 0.10}

# —— 攻略层实证补位（M6-B 与 F-F2 美食榜的接线）——
# 餐厅候选常因逐点验证撞风控而拿不到独立视频：heat_score=0、evidence 空，
# 美食榜推荐分会全部并列（实测 5 家全 0.24），排序失去意义。而城市攻略层
# 已经证明这些店被高赞攻略真实提及过，这份证据不该浪费。
_GUIDE_HEAT_W = {"高": 1.0, "中": 0.6, "低": 0.3}
GUIDE_HEAT_DISCOUNT = 0.7   # 攻略提及弱于独立实测，计入热度维度时打折


def _fuzzy_mention(name: str, by_name: dict) -> dict | None:
    """攻略提及名与候选名不完全一致时做包含匹配（如"莲芳蹄花店"↔"莲芳蹄花"）。

    限 3 字以上才参与包含匹配，避免短名误伤。"""
    n = str(name or "").strip()
    if len(n) < 3:
        return None
    for k, v in by_name.items():
        if len(k) >= 3 and (k in n or n in k):
            return v
    return None


def apply_guide_evidence(catalog: dict, guide: dict | None) -> dict:
    """把攻略层提炼的 heat/note 补进 catalog 详情（返回新 dict，不改入参）。

    原则：①只补空位，已有实测数据一律不覆盖（UGC 实测优先于攻略提及）；
    ②注入字段带 guide_ 前缀，渲染层可标注"攻略提及"来源，不与实测混淆。
    无攻略数据时原样返回（kernel-only / 攻略层未启用时零回归）。纯函数可测。"""
    mentions = (guide or {}).get("guide_candidates") or []
    if not mentions or not catalog:
        return catalog
    by_name: dict[str, dict] = {}
    for m in mentions:
        nm = str((m or {}).get("name") or "").strip()
        if nm and nm not in by_name:
            by_name[nm] = m
    out = dict(catalog)
    for section in ("poi", "food"):
        sec = dict(catalog.get(section) or {})
        hit = False
        for name, det in sec.items():
            m = by_name.get(str(name).strip()) or _fuzzy_mention(name, by_name)
            if not m:
                continue
            d = dict(det or {})
            if str(m.get("heat") or "").strip():
                d.setdefault("guide_heat", str(m["heat"]).strip())
            if str(m.get("note") or "").strip():
                d.setdefault("guide_note", str(m["note"]).strip())
            sec[name] = d
            hit = True
        if hit:
            out[section] = sec
    return out


def food_score_breakdown(det: dict) -> dict:
    """美食详情 → 各维归一化得分（F-F2，可复算）：
    evidence 好评证据强度 / price 人均信息完备度（有值且≥2样本最高）/
    heat 抖音热度 / reputation 口碑标签（老字号加分、网红扣分）/ queue 排队风险低加分。纯函数可测。"""
    ev = str(det.get("evidence") or det.get("positive_density") or "")
    ev_s = next((w for k, w in _EVIDENCE_W.items() if k in ev), 0.3)
    avg, n = det.get("avg_price"), int(det.get("price_samples_n") or 0)
    price_s = 1.0 if (avg is not None and n >= 2) else (0.6 if avg is not None else 0.0)
    heat_s = min(1.0, max(0.0, float(det.get("heat_score") or 0.0)))
    # 攻略层实证兜底：逐点验证撞风控拿不到独立视频时 heat_score=0，但高赞攻略
    # 真实提及过它——按提及热度打折计入（弱于独立实测，故不与实测同权）
    gw = _GUIDE_HEAT_W.get(str(det.get("guide_heat") or "").strip())
    if gw is not None:
        heat_s = max(heat_s, round(gw * GUIDE_HEAT_DISCOUNT, 2))
    tags = " ".join(str(t) for t in (det.get("tags") or []))
    rep_s = 0.6
    if any(k in tags for k in _REPUTATION_POS):
        rep_s += 0.2
    if any(k in tags for k in _REPUTATION_NEG):
        rep_s -= 0.2
    q = str(det.get("queue_risk") or "")
    queue_s = next((w for k, w in _QUEUE_W.items() if k in q), 0.5)
    return {"evidence": round(ev_s, 2), "price": round(price_s, 2), "heat": round(heat_s, 2),
            "reputation": round(max(0.0, min(1.0, rep_s)), 2), "queue": round(queue_s, 2)}


def food_rank_score(det: dict) -> float:
    """美食榜推荐分 = 多维加权和（权重见 _FOOD_WEIGHTS，与 breakdown 对应，可复算）。纯函数可测。"""
    bd = food_score_breakdown(det)
    return round(sum(_FOOD_WEIGHTS[k] * v for k, v in bd.items()), 3)


def to_food_rank_items(decisions: list[SpotDecision], catalog: dict,
                       *, limit: int | None = None) -> list[dict]:
    """美食榜 RankItem：按 food_rank_score 降序（不再蹭景点热度口径），
    score_breakdown 展开各维得分（可复算），ref_id 与 catalog.food 同名对齐（R11 无死链）。纯函数可测。"""
    food_cat = catalog.get("food") or {}
    pool = [d for d in decisions if d.is_food and d.name in food_cat]
    pool.sort(key=lambda d: (-food_rank_score(food_cat.get(d.name) or {}), d.name))
    if limit:
        pool = pool[:limit]
    out: list[dict] = []
    for i, d in enumerate(pool, start=1):
        det = food_cat.get(d.name) or {}
        not_sel = "" if d.decision.state == STATE_IN else (d.decision.reason or "")
        out.append(RankItem(
            ref_id=d.name, kind=RANK_KIND_FOOD, rank=i, score=food_rank_score(det),
            score_breakdown=food_score_breakdown(det), trend=d.heat.trend or "",
            state=d.decision.state or "", not_selected_reason=not_sel,
            tags=_tags_for(d),
        ).to_dict())
    return out


def build_intro(*, city: str, days: int, decisions: list[SpotDecision],
                legs: list[dict] | None = None) -> list[dict]:
    """Epic E 攻略介绍：只读证据对象、facts_ref 挂引用、不新造票价/时间数字。纯函数可测。"""
    sel = [d for d in decisions if d.decision.state == STATE_IN and not d.is_food]
    alt = [d for d in decisions if d.decision.state == STATE_ALT and not d.is_food]
    intro: list[IntroSection] = []
    # 目的地总览
    intro.append(IntroSection(
        key="overview", title=f"{city} 行程总览",
        body=(f"{city} {days} 天行程，覆盖 {len(sel)} 个入选点、{len(alt)} 个备选；"
              "点位数与天数由决策层排定，具体票价/开放时间见各点详情以官方为准。"),
        facts_ref=[d.name for d in sel + alt],
    ))
    # 抵达与市内交通（只列方式，不报价格区间）
    modes = sorted({str(lg.get("mode") or "").strip() for lg in (legs or []) if lg.get("mode")})
    intro.append(IntroSection(
        key="transport", title="抵达与市内交通",
        body=("本方案涉及的交通方式：" + "、".join(modes) if modes
              else "交通方式待配置地图或补充调研后明确。"),
        facts_ref=[f"{lg.get('from')}->{lg.get('to')}" for lg in (legs or [])][:12],
    ))
    # 整体避坑（取各点 avoid 的少量代表，附来源点位）
    pitfalls = [(d.name, a) for d in sel for a in (d.profile.get("avoid") or [])[:2]][:6]
    intro.append(IntroSection(
        key="avoid", title="整体避坑",
        body=("；".join(f"{n}：{a}" for n, a in pitfalls) if pitfalls
              else "暂无跨点共性避坑（单点避坑见行程与详情）。"),
        facts_ref=sorted({n for n, _ in pitfalls}),
    ))
    # 入选点速览（一句话，来自已调研 highlights，不与详情大段重复）
    glance = [f"{d.name}：{_first_text(d.profile, 'intro') or ((d.profile.get('highlights') or [''])[0])}"
              for d in sel if d.name]
    intro.append(IntroSection(
        key="spots", title="入选点速览",
        body=("。".join(s for s in glance if not s.endswith("：")) if glance
              else "入选点速览待补充。"),
        facts_ref=[d.name for d in sel],
    ))
    return [s.to_dict() for s in intro]


@dataclass
class TripPlan:
    """决策层唯一顶层对象（§6.11）。三端只读它渲染，同一事实全局同源。"""
    meta: dict = field(default_factory=dict)
    intro: list[dict] = field(default_factory=list)
    itinerary: list[dict] = field(default_factory=list)
    heat_ranking: list[dict] = field(default_factory=list)
    food_ranking: list[dict] = field(default_factory=list)
    catalog: dict = field(default_factory=lambda: {"poi": {}, "food": {}})
    quality: dict = field(default_factory=dict)
    decision_table: list[dict] = field(default_factory=list)
    plan_b: list[str] = field(default_factory=list)
    to_verify: list[str] = field(default_factory=list)
    appendix: dict = field(default_factory=dict)
    snap: dict = field(default_factory=dict)   # 呈现快照：决策层预先算好的渲染输入（表达层零计算）

    def to_dict(self) -> dict:
        d = asdict(self)
        d["catalog"].pop("_food_names", None)
        return d


def build_trip_plan(*, meta: dict, decisions: list[SpotDecision], plan: dict,
                    quality=None,
                    legs: list[dict] | None = None,
                    pitfall_by_spot: dict[str, list[dict]] | None = None,
                    ranking_limit: int | None = None,
                    food_min_samples: int | None = None,
                    snap: dict | None = None,
                    guide: dict | None = None) -> TripPlan:
    """从已有统一对象投影出 TripPlan（M4a：双写新增，不改/不替换现有渲染）。纯函数可测。

    票价同源：先建 catalog（权威价），再据此回填 itinerary 每槽的 ticket_price/open_hours，
    使 R12 能断言「行程/详情同一事实一致」（构造即一致，任一处被改动即暴露）。

    guide（M6-B）：城市攻略层提炼结果；其 heat/note 在算榜单之前补进 catalog，
    让因风控拿不到独立视频的餐厅在美食榜仍有区分度（不覆盖已有实测数据）。"""
    catalog = apply_guide_evidence(
        build_catalog(decisions, legs=legs, pitfall_by_spot=pitfall_by_spot), guide)
    poi_cat = catalog.get("poi") or {}
    # itinerary：深拷贝槽位并从 catalog 注入同源票价/开放时间
    itinerary: list[dict] = []
    for day in (plan or {}).get("days") or []:
        blocks = []
        for s in day.get("slots") or []:
            b = dict(s)
            det = poi_cat.get(str(s.get("spot") or ""))
            if det:
                b["ticket_price"] = det.get("ticket_price")
                b["open_hours"] = det.get("open_hours") or ""
            blocks.append(b)
        itinerary.append({**{k: v for k, v in day.items() if k != "slots"}, "blocks": blocks})
    heat_ranking = to_rank_items(decisions, catalog, kind=RANK_KIND_POI, limit=ranking_limit)
    food_ranking = to_food_rank_items(decisions, catalog, limit=ranking_limit)
    # F-F2：样本低于配置下限时显式说明而非静默只列几家（宁缺不编）
    snap_out = dict(snap or {})
    if food_min_samples and len(food_ranking) < food_min_samples:
        snap_out["food_sample_note"] = (
            f"美食候选仅调研到 {len(food_ranking)} 家，低于样本下限 {food_min_samples} 家；"
            "榜单按已调研样本排序，建议出发前结合本地美食榜单补充。")
    intro = build_intro(city=str((meta or {}).get("city") or ""), days=int(_num((meta or {}).get("days"), 1)),
                        decisions=decisions, legs=legs)
    sources = sorted({u for d in decisions for u in d.sources})
    qd = quality.to_dict() if hasattr(quality, "to_dict") else (quality if isinstance(quality, dict) else {})
    return TripPlan(
        meta=dict(meta or {}), intro=intro, itinerary=itinerary,
        heat_ranking=heat_ranking, food_ranking=food_ranking,
        catalog={"poi": poi_cat, "food": catalog.get("food") or {}},
        quality=qd,
        decision_table=[d.to_row() for d in decisions],
        plan_b=[d.decision.reason for d in decisions if d.decision.state == STATE_ALT and d.decision.reason][:8],
        to_verify=sorted({d.name for d in decisions
                          if d.decision.state == STATE_IN and not d.official.source_url}),
        appendix={"sources": sources},
        snap=snap_out,
    )
