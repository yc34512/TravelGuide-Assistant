"""规划质量门禁（PRD Epic 5 / F5.1，M1 技术核心）。

独立于生成模型的确定性规则集：规划产出后逐条裁判 R1~R10，产出可展示的
QualityReport。生成与裁判分离——门禁只读统一决策对象（pipeline.decision）与
规划结果，不调用 LLM、不改结论；发现问题回传修正指令（issues）供有限回炉使用，
回炉后仍不达标则进 unresolved（已知妥协），由表达层显式列出，绝不静默。

纯函数、可离线断言：所有规则对缺失数据保守处理——无数据一律标 skip（不冒充
pass，也不无端 fail），绝不抛异常中断主流程。

M1 落地 R1/R2/R3/R4/R6/R7/R8/R10（当前数据即可判定）；R5（动线，F3.3）与
R9（时效，Epic 6）依赖 M2 数据，无数据时如实标 skip，接数据后自动生效。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.decision import (
    EVIDENCE_STRONG,
    HARD_REASONS,
    STATE_ALT,
    STATE_IN,
    SpotDecision,
    official_expired,
)

# —— 裁判状态（PRD §5.3：pass/warn/fail，另加 skip 表"本期无数据未判定"）——
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"

# —— 门禁阈值（集中便于调参，与 planner._coverage_issues / trip 口径一致）——
MIN_SLOTS_PER_DAY = 2       # 每天合理下限（同 _coverage_issues：min(2*days, 档案数)）
MAX_SLOTS_PER_DAY = 4       # 体力上限：单日超过判 warn
DAY_AVAILABLE_HOURS = 11.0  # 单日游玩可用时间窗（上午 4 + 下午 4 + 晚上 3）
MEAL_HOURS = 1.0            # 每餐占用小时
TOP_N_HEAT = 3              # 热度前 N 视为高价值点（R2）


@dataclass
class Check:
    """单条规则的裁判结果。"""

    rule_id: str
    name: str
    status: str
    actual: str = ""    # 实测值（展示 + 回归对比）
    note: str = ""      # 说明
    fix: str = ""       # 修正指令（fail/warn 时喂给回炉）

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "name": self.name, "status": self.status,
                "actual": self.actual, "note": self.note, "fix": self.fix}


@dataclass
class QualityReport:
    """质量门禁产物（PRD §5.3，必须可展示、可落库、可离线断言）。"""

    checks: list[Check] = field(default_factory=list)
    repair_rounds: int = 0
    unresolved: list[str] = field(default_factory=list)

    @property
    def fails(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warns(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def issues(self) -> list[str]:
        """喂给有限回炉的修正指令（fail 优先，warn 次之，保留规则顺序）。"""
        return [c.fix for c in self.checks if c.status in (FAIL, WARN) and c.fix]

    @property
    def score(self) -> int:
        """质量总分 0~100：pass=1、warn=0.5、fail=0；skip 不计入分母（未判定不奖惩）。"""
        countable = [c for c in self.checks if c.status != SKIP]
        if not countable:
            return 100
        earned = sum(1.0 if c.status == PASS else 0.5 if c.status == WARN else 0.0
                     for c in countable)
        return round(100 * earned / len(countable))

    @property
    def passed(self) -> bool:
        """无 fail 即视为通过（warn 不阻断，但要显式呈现）。"""
        return not self.fails

    def problem_count(self) -> int:
        """回炉比较口径：fail 计 2、warn 计 1。问题数严格变少才采纳新版本（F5.2）。"""
        return sum(2 if c.status == FAIL else 1 if c.status == WARN else 0
                   for c in self.checks)

    def get(self, rule_id: str) -> Check | None:
        for c in self.checks:
            if c.rule_id == rule_id:
                return c
        return None

    def to_dict(self) -> dict:
        return {"checks": [c.to_dict() for c in self.checks], "score": self.score,
                "issues": self.issues, "repair_rounds": self.repair_rounds,
                "unresolved": self.unresolved, "passed": self.passed}


def finalize(report: QualityReport, repair_rounds: int = 0) -> QualityReport:
    """回炉结束后定稿：仍 fail 的项写入 unresolved（已知妥协），供表达层显式列出。

    PRD F5.3：回炉后仍 fail 的项进 unresolved，配原因与可选动作，绝不静默。
    warn 不列入 unresolved（软提示，仍在 checks 中可见）。"""
    report.repair_rounds = repair_rounds
    report.unresolved = [f"[{c.rule_id} {c.name}] {c.note}" for c in report.checks
                         if c.status == FAIL]
    return report


def _planned_slots(plan: dict) -> list[dict]:
    return [s for d in (plan or {}).get("days", []) for s in d.get("slots", [])]


# —— R1 覆盖密度：入选点过少（P-1 地标静默丢弃）或过密（体力透支）——
def _r1_coverage(plan: dict, profiles: dict, days: int) -> Check:
    n = len(_planned_slots(plan))
    d = max(1, int(days or 1))
    n_profiles = len(profiles or {})
    min_slots = min(MIN_SLOTS_PER_DAY * d, n_profiles) if n_profiles else MIN_SLOTS_PER_DAY * d
    max_slots = MAX_SLOTS_PER_DAY * d
    if n < min_slots:
        return Check("R1", "覆盖密度", FAIL, f"入选 {n} 点 / 下限 {min_slots}",
                     "行程点过少，已调研景点未充分排入",
                     f"请把更多已调研景点排入行程，全程至少 {min_slots} 个点位")
    if n > max_slots:
        return Check("R1", "覆盖密度", WARN, f"入选 {n} 点 / 上限 {max_slots}",
                     "行程过密，可能体力透支",
                     "适当减少每日点位，为通勤与休息留出余量")
    return Check("R1", "覆盖密度", PASS, f"入选 {n} 点（合理区间 {min_slots}~{max_slots}）",
                 "覆盖密度合理")


# —— R2 高价值点零静默丢弃：热度 TopN 或强证据点未入选，必须有硬理由（闭集）——
def _r2_no_silent_drop(decisions: list[SpotDecision], top_n: int = TOP_N_HEAT) -> Check:
    measured = [d for d in decisions if d.heat.measured]
    top = {d.name for d in sorted(measured, key=lambda x: -x.heat.score)[:max(0, top_n)]}
    offenders = []
    for d in decisions:
        if d.decision.state == STATE_IN or d.is_food:
            continue   # 已入选，或美食（餐厅覆盖由 R7 负责，不进行程骨架判定）
        if not (d.verify.evidence == EVIDENCE_STRONG or d.name in top):
            continue
        if d.decision.has_hard_reason:
            continue
        offenders.append(d.name)
    if offenders:
        return Check("R2", "高价值点零静默丢弃", FAIL,
                     f"{len(offenders)} 个：{'、'.join(offenders[:5])}",
                     "高热度/强证据点未入选且理由不属于硬理由闭集",
                     f"以下点必须排入行程，或在理由中明确给出硬理由之一"
                     f"（{'／'.join(HARD_REASONS)}）：{'、'.join(offenders[:5])}")
    return Check("R2", "高价值点零静默丢弃", PASS, "无静默丢弃",
                 "高价值点均已入选或给出硬理由")


# —— R3 时段/开闭园：晚上型必排晚上；有官方闭馆日 + 周几时校验闭馆日 ——
def _r3_time_slot(plan: dict, profiles: dict, decisions: list[SpotDecision],
                  day_weekdays: list[str] | None = None) -> Check:
    problems = []
    for s in _planned_slots(plan):
        p = profiles.get(s.get("spot")) or {}
        if p.get("best_time_slot") == "晚上" and s.get("slot") != "晚上":
            problems.append(f"{s.get('spot')}（最佳晚上却排{s.get('slot')}）")
    if day_weekdays:
        by_name = {d.name: d for d in decisions}
        for day in (plan or {}).get("days", []):
            try:
                idx = int(day.get("day", 0)) - 1
            except (TypeError, ValueError):
                continue
            wd = day_weekdays[idx] if 0 <= idx < len(day_weekdays) else ""
            for s in day.get("slots", []):
                dec = by_name.get(s.get("spot"))
                close = (dec.official.close_day if dec else "") or ""
                if wd and close and close in wd:
                    problems.append(f"{s.get('spot')}（{wd}闭馆）")
    if problems:
        return Check("R3", "时段/开闭园", FAIL, f"{len(problems)} 处：{'、'.join(problems[:4])}",
                     "时段错配或排入闭馆日",
                     f"把晚上型景点排到晚上时段、避开闭馆日：{'、'.join(problems[:4])}")
    return Check("R3", "时段/开闭园", PASS, "无时段冲突",
                 "晚上型已排晚上" + ("，闭馆日已校验" if day_weekdays else ""))


# —— R4 时间可行：单日已知游玩时长 + 用餐超出可用时间窗 ——
def _r4_time_feasible(plan: dict, profiles: dict) -> Check:
    over = []
    for day in (plan or {}).get("days", []):
        total, known, meals = 0.0, 0, 0
        for s in day.get("slots", []):
            dur = (profiles.get(s.get("spot")) or {}).get("duration_hours")
            try:
                if dur:
                    total += float(dur)
                    known += 1
            except (TypeError, ValueError):
                pass
            if str(s.get("food") or "").strip():
                meals += 1
        total += MEAL_HOURS * min(meals, 2)
        if known and total > DAY_AVAILABLE_HOURS:
            over.append(f"第{day.get('day')}天约{total:.1f}h")
    if over:
        return Check("R4", "时间可行", FAIL, "；".join(over),
                     "单日游玩+用餐超出可用时间窗",
                     f"单日安排不超过约 {DAY_AVAILABLE_HOURS:.0f} 小时，请精简当天点位：{'；'.join(over)}")
    return Check("R4", "时间可行", PASS, "各日时长可行", "未超出单日可用时间窗")


# —— R5 动线：折返检测（F3.3）在 M2 落地，本期如实标 skip ——
def _r5_route(plan: dict, locs: dict | None = None) -> Check:
    return Check("R5", "动线", SKIP,
                 "已提供坐标" if locs else "无坐标数据",
                 "同日折返检测属 F3.3 动线优化（M2），本期不判定")


# —— R6 预算：超支且未调整判 fail；有票价缺口判 warn；未设预算 skip ——
def _r6_budget(budget_summary: dict | None) -> Check:
    bs = budget_summary or {}
    if not bs.get("total_budget"):
        return Check("R6", "预算", SKIP, "未设用户预算", "无预算约束，不判定超支")
    total = float(bs.get("total") or 0)
    tb = float(bs.get("total_budget") or 0)
    if bs.get("status") == "超支":
        return Check("R6", "预算", FAIL, f"预估 {total:.0f} / 预算 {tb:.0f}",
                     f"估算超预算约 {total - tb:.0f} 元且未做调整",
                     f"预估 {total:.0f} 元超出预算 {tb:.0f} 元，请减少付费景点、选免费替代或降低餐饮标准")
    missing = bs.get("tickets_missing") or []
    if missing:
        return Check("R6", "预算", WARN, f"预估 {total:.0f} / 预算 {tb:.0f}",
                     f"{'、'.join(missing[:4])} 无票价数据，总额可能偏低",
                     "出发前核实这些景点票价，预算总额可能上调")
    return Check("R6", "预算", PASS, f"预估 {total:.0f} / 预算 {tb:.0f}", "预算内，口径自洽")


# —— R7 餐饮：编造店名直接 fail；有候选却漏排 warn；无候选且无兜底 skip ——
def _r7_food(plan: dict, food_profiles: dict | None, food_fallback: str | None = None) -> Check:
    slots = _planned_slots(plan)
    candidates = set(food_profiles or {})
    fabricated = []
    for s in slots:
        ftext = str(s.get("food") or "").strip()
        if ftext and candidates and not any(c in ftext for c in candidates):
            fabricated.append(ftext[:20])
    if fabricated:
        return Check("R7", "餐饮", FAIL, f"疑似编造：{'、'.join(fabricated[:3])}",
                     "餐食推荐含候选清单外的店名",
                     "只能从已调研餐厅候选中推荐，或给'品类+人均区间'兜底，严禁编造店名")
    if not candidates and not food_fallback:
        return Check("R7", "餐饮", SKIP, "无餐厅调研数据",
                     "F4.6 基线兜底未接入时无从推荐午/晚餐")
    empty_days = []
    for day in (plan or {}).get("days", []):
        if not day.get("slots"):
            continue
        has_food = any(str(s.get("food") or "").strip() for s in day.get("slots", []))
        if not has_food and not food_fallback:
            empty_days.append(f"第{day.get('day')}天")
    if empty_days:
        return Check("R7", "餐饮", WARN, f"{len(empty_days)} 天缺餐食推荐：{'、'.join(empty_days[:3])}",
                     "有餐厅候选却未排入当天午/晚餐",
                     f"为有行程的当天补午/晚餐推荐或基线兜底：{'、'.join(empty_days[:3])}")
    return Check("R7", "餐饮", PASS, "餐食推荐完备", "各日午/晚餐已推荐或有兜底说明")


# —— R8 避坑归属：避坑条目的来源视频不属于任何入选/备选点则 fail ——
def _r8_pitfall_attribution(pitfall: list[dict] | None, decisions: list[SpotDecision]) -> Check:
    rows = pitfall or []
    if not rows:
        return Check("R8", "避坑归属", SKIP, "无避坑条目", "")
    url_state: dict[str, str] = {}
    for d in decisions:
        for u in d.sources:
            url_state[str(u)] = d.decision.state
    orphan, checked = [], 0
    for row in rows:
        src = str(row.get("source") or "")
        if not src or src not in url_state:
            continue
        checked += 1
        if url_state[src] not in (STATE_IN, STATE_ALT):
            orphan.append(str(row.get("claim", ""))[:20])
    if not checked:
        return Check("R8", "避坑归属", SKIP, "避坑条目无可归属来源", "")
    if orphan:
        return Check("R8", "避坑归属", FAIL, f"{len(orphan)} 条属于未入选点",
                     "避坑专题混入了未入选/备选点的内容",
                     f"避坑只保留入选/备选点，其余移入'备选点风险'分区：{'、'.join(orphan[:3])}")
    return Check("R8", "避坑归属", PASS, f"{checked} 条均可归属", "避坑条目都属于入选/备选点")


# —— R9 时效：官方事实过期（Epic 6）——无官方数据时 skip ——
def _r9_timeliness(decisions: list[SpotDecision], today: str | None = None) -> Check:
    facts = [d for d in decisions if not d.official.missing]
    if not facts:
        return Check("R9", "时效", SKIP, "无官方事实数据",
                     "Epic 6 时效治理在 M2 落地，本期未启用")
    expired = [d.name for d in facts if today and official_expired(d.official, today)]
    if expired:
        return Check("R9", "时效", WARN, f"{len(expired)} 点官方事实已过期",
                     "引用了已过期的官方信息（票价/活动/营业时间）",
                     f"核实并更新，过期项不得作为亮点：{'、'.join(expired[:4])}")
    return Check("R9", "时效", PASS, "官方事实均在有效期", "")


# —— R10 来源完备：入选点缺可溯源来源则 fail ——
def _r10_sources(decisions: list[SpotDecision]) -> Check:
    missing = [d.name for d in decisions if d.decision.state == STATE_IN and not d.sources]
    n_in = sum(1 for d in decisions if d.decision.state == STATE_IN)
    if missing:
        return Check("R10", "来源完备", FAIL, f"{len(missing)} 个入选点无来源：{'、'.join(missing[:4])}",
                     "入选点缺可溯源来源",
                     f"为这些点补溯源链接或从行程移除：{'、'.join(missing[:4])}")
    return Check("R10", "来源完备", PASS, f"{n_in} 个入选点均有来源", "溯源完备")


def run_quality_gate(*, decisions: list[SpotDecision], plan: dict, profiles: dict,
                     days: int, budget_summary: dict | None = None,
                     pitfall: list[dict] | None = None,
                     food_profiles: dict | None = None,
                     day_weekdays: list[str] | None = None,
                     today: str | None = None, food_fallback: str | None = None,
                     locs: dict | None = None, top_n: int = TOP_N_HEAT) -> QualityReport:
    """对一份规划跑完整门禁，产出 QualityReport。纯函数、只读、不调 LLM。

    入参全部来自 service.trip 在'生成规划'后已有的数据（profiles/plan/budget_summary/
    heat_rows/pitfall/sources）与 build_decisions 的统一对象；缺什么对应规则就 skip。"""
    decisions = decisions or []
    plan = plan or {}
    profiles = profiles or {}
    checks = [
        _r1_coverage(plan, profiles, days),
        _r2_no_silent_drop(decisions, top_n),
        _r3_time_slot(plan, profiles, decisions, day_weekdays),
        _r4_time_feasible(plan, profiles),
        _r5_route(plan, locs),
        _r6_budget(budget_summary),
        _r7_food(plan, food_profiles, food_fallback),
        _r8_pitfall_attribution(pitfall, decisions),
        _r9_timeliness(decisions, today),
        _r10_sources(decisions),
    ]
    return QualityReport(checks=checks)
