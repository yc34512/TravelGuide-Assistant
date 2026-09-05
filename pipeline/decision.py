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

from dataclasses import dataclass, field

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
    "预算超限",
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
        """完全无官方数据：渲染层据此标"待核实"，预算层据此走 UGC 区间占位。"""
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
