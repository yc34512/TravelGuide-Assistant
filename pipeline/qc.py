"""规划质量门禁（PRD Epic 5 / F5.1，M1 技术核心）。

独立于生成模型的确定性规则集：规划产出后逐条裁判 R1~R10，产出可展示的
QualityReport。生成与裁判分离——门禁只读统一决策对象（pipeline.decision）与
规划结果，不调用 LLM、不改结论；发现问题回传修正指令（issues）供有限回炉使用，
回炉后仍不达标则进 unresolved（已知妥协），由表达层显式列出，绝不静默。

纯函数、可离线断言：所有规则对缺失数据保守处理——无数据一律标 skip（不冒充
pass，也不无端 fail），绝不抛异常中断主流程。

M1 落地 R1/R2/R3/R4/R7/R8/R10（当前数据即可判定）；R5（动线，F3.3）与
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
R5_RATIO = 1.9             # 单日绕行比超此值判折返（实际路径/首尾直达）
R5_MIN_EXCESS_KM = 3.0     # 绕行绝对超出下限，避免近距离抖动误报


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


# —— R1 覆盖密度：天数不完整 / 入选点过少（地标静默丢弃）/ 过密（体力透支）；全天型独占一天为合法（F-D1）——
def _r1_coverage(plan: dict, profiles: dict, days: int) -> Check:
    from pipeline.planner import is_all_day
    d = max(1, int(days or 1))
    plan_days = (plan or {}).get("days") or []
    slots = _planned_slots(plan)
    # 入选点数按去重点位计：同一点被重复排入不应虚增覆盖密度（曾把 6 槽报成“入选 6 点”）
    n = len({str(s.get("spot") or "") for s in slots if s.get("spot")})
    # 天数完整性优先于点位数判定：用户要 N 天就必须有 N 天安排。素材不足可以降低
    # 每日点数（下方 min_slots 已按档案数放宽），但绝不能静默少一整天——实测出现过
    # “要 3 天只排 2 天”而本规则因点位总数达标报 pass（门禁全绿不等于没问题）。
    empty_days = [i + 1 for i, dd in enumerate(plan_days) if not (dd.get("slots") or [])]
    if len(plan_days) < d or empty_days:
        miss = (f"仅输出 {len(plan_days)} 天" if len(plan_days) < d
                else f"第 {'、'.join(str(x) for x in empty_days)} 天无任何安排")
        return Check("R1", "覆盖密度", FAIL, f"要求 {d} 天，{miss}（已排 {n} 点）",
                     "行程天数不完整：存在整天空白，用户拿到的天数少于要求",
                     f"必须输出全部 {d} 天的 days 数组、每天至少一个时段有安排；"
                     f"点位不足时把已调研点分散到每一天（如 2+1+1），不得空整天")
    drops = (plan or {}).get("duplicate_drops") or []   # _normalize_plan 剔除的重复排入记录
    n_profiles = len(profiles or {})
    all_day_days = 0        # 合法“1 点独占整天”的天数
    shared_all_day: list[str] = []   # 全天型点却与其他点同日（未独占）
    for day in (plan or {}).get("days", []):
        spots = [s.get("spot") for s in day.get("slots", []) if s.get("spot")]
        ad = [sp for sp in spots if is_all_day((profiles or {}).get(sp))]
        if len(spots) == 1 and ad:
            all_day_days += 1
        elif ad and len(spots) >= 2:
            shared_all_day.extend(ad)
    # 下限：非全天日按 2/天、全天独占日按 1/天（扣除全天日数）
    expected = max(1, MIN_SLOTS_PER_DAY * d - all_day_days)
    min_slots = min(expected, n_profiles) if n_profiles else expected
    max_slots = MAX_SLOTS_PER_DAY * d
    if n < min_slots:
        return Check("R1", "覆盖密度", FAIL, f"入选 {n} 点 / 下限 {min_slots}",
                     "行程点过少，已调研景点未充分排入",
                     f"请把更多已调研景点排入行程，全程至少 {min_slots} 个点位")
    if n > max_slots:
        return Check("R1", "覆盖密度", WARN, f"入选 {n} 点 / 上限 {max_slots}",
                     "行程过密，可能体力透支",
                     "适当减少每日点位，为通勤与休息留出余量")
    if drops:
        return Check("R1", "覆盖密度", WARN,
                     f"入选 {n} 点；已剔除重复排入 {len(drops)} 处：{'、'.join(drops[:3])}",
                     "同一点位被排进了多天/多时段（每点全程只应排一次）",
                     "重复槽位已按首次出现保留、其余剔除；如某天因此变空，请补排其他已调研景点")
    # 排布过空（实测出现过“第 2 天只有上午”）：除全天型独占日外每天至少两个时段；
    # 只在素材够分（去重入选点 ≥ 2×天数 − 全天独占日数）时判 FAIL，点数本不够摊时留已知妥协
    thin_days: list[int] = []
    for i, dd in enumerate(plan_days, start=1):
        sl = dd.get("slots") or []
        if len(sl) == 1 and is_all_day((profiles or {}).get(str(sl[0].get("spot") or ""))):
            continue   # 全天型点独占一天：单条 slot="全天" 为合法排法
        if len(sl) <= 1:
            thin_days.append(i)
    if thin_days and n >= 2 * len(plan_days) - all_day_days:
        return Check("R1", "覆盖密度", FAIL,
                     f"第 {'、'.join(str(x) for x in thin_days)} 天仅 1 个时段（共 {n} 点）",
                     "排布过空：存在只有单个时段的日子",
                     f"把排布过满天里的点位匀给第 {'、'.join(str(x) for x in thin_days)} 天，"
                     f"使每天至少两个时段有安排（全天型独占日除外）")
    if shared_all_day:
        return Check("R1", "覆盖密度", WARN, f"全天型未独占：{'、'.join(shared_all_day[:3])}",
                     "建议独占一整天的全天型点与其他点排在同一天",
                     "把全天型点（如主题乐园）单独安排一整天，避免与其他景点同日挤占")
    return Check("R1", "覆盖密度", PASS,
                 f"入选 {n} 点（合理区间 {min_slots}~{max_slots}）"
                 + (f"，含 {all_day_days} 个全天独占日" if all_day_days else ""),
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


# —— R4 时间可行：单日已知游玩时长 + 用餐 + 实测通勤超出可用时间窗 ——
def _r4_time_feasible(plan: dict, profiles: dict, legs: list[dict] | None = None) -> Check:
    leg_min: dict = {}
    for lg in legs or []:
        if lg.get("nature") == "实测" and lg.get("minutes"):
            leg_min[lg.get("day")] = leg_min.get(lg.get("day"), 0.0) + float(lg["minutes"])
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
        commute = leg_min.get(day.get("day"), 0.0) / 60.0   # 只计实测通勤，无实测不臆造
        total += commute
        if known and total > DAY_AVAILABLE_HOURS:
            over.append(f"第{day.get('day')}天约{total:.1f}h" + (f"（含通勤{commute:.1f}h）" if commute else ""))
    if over:
        return Check("R4", "时间可行", FAIL, "；".join(over),
                     "单日游玩+用餐（+实测通勤）超出可用时间窗",
                     f"单日安排不超过约 {DAY_AVAILABLE_HOURS:.0f} 小时，请精简当天点位：{'；'.join(over)}")
    return Check("R4", "时间可行", PASS, "各日时长可行", "未超出单日可用时间窗")


# —— R5 动线：同日折返/绕路检测（F-D3）——需坐标；无坐标如实 skip（不假 pass/fail）——
def _r5_route(plan: dict, locs: dict | None = None) -> Check:
    if not locs:
        return Check("R5", "动线", SKIP, "无坐标数据",
                     "未启用高德/无坐标，无法判定动线折返（配 Key 后自动生效）")
    from core import geo
    warnings = []
    for day in (plan or {}).get("days", []):
        pts = [locs[s.get("spot")] for s in day.get("slots", []) if locs.get(s.get("spot"))]
        if len(pts) < 3:
            continue
        path = 0.0
        for a, b in zip(pts, pts[1:]):
            dd = geo.distance_km(a, b)
            if dd is None:
                path = -1.0
                break
            path += dd
        if path < 0:
            continue
        net = geo.distance_km(pts[0], pts[-1]) or 0.0
        if net < 0.3:        # 首尾近乎重合（环线）不判折返
            continue
        excess = path - net
        if path > R5_RATIO * net and excess > R5_MIN_EXCESS_KM:
            warnings.append(f"第{day.get('day')}天绕行{path:.1f}km（直达仅{net:.1f}km）")
    if warnings:
        return Check("R5", "动线", WARN, "；".join(warnings),
                     "同日存在明显折返/绕路",
                     "按地理就近串联同日点位，减少来回穿越市区：" + "；".join(warnings))
    return Check("R5", "动线", PASS, "同日动线无明显折返", "动线顺序与地理一致")


# —— R7 餐饮：编造店名直接 fail（餐厅调研与时间线解耦，时间线一律不排店名）——
def _r7_food(plan: dict, food_profiles: dict | None) -> Check:
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
                     "只能从已调研餐厅候选中推荐，严禁编造店名")
    if not candidates:
        return Check("R7", "餐饮", SKIP, "无餐厅调研数据", "未调研到餐厅，本期无从校验餐饮推荐")
    return Check("R7", "餐饮", PASS,
                 f"已调研 {len(candidates)} 家餐厅；时间线不排餐厅（解耦），未发现编造店名", "")


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


# —— R11 榜单无死链 / R12 两处同源：catalog 驱动的一致性（F-C4，M4 接真实判定）——
def _r11_no_dead_link(trip_plan: dict | None = None) -> Check:
    """R11（§6.8/§11）：每个 RankItem.ref_id 必须在 catalog 有唯一详情；无 trip_plan 则 skip。"""
    if not trip_plan:
        return Check("R11", "榜单无死链", SKIP, "未传 trip_plan（M4a 契约就绪，待 service 接线）",
                     "榜单→详情映射未接入，本期不判定；传入 trip_plan 后自动生效")
    cat = trip_plan.get("catalog") or {}
    poi_cat = cat.get("poi") or {}
    food_cat = cat.get("food") or {}
    ranks = (trip_plan.get("heat_ranking") or []) + (trip_plan.get("food_ranking") or [])
    if not ranks:
        return Check("R11", "榜单无死链", SKIP, "无榜单项", "本期无榜单数据，不判定")
    dead = [str(r.get("ref_id") or "") for r in ranks
            if not ((r.get("kind") == "food" and r.get("ref_id") in food_cat)
                    or (r.get("kind") != "food" and r.get("ref_id") in poi_cat))]
    if dead:
        return Check("R11", "榜单无死链", FAIL, f"{len(dead)} 个榜单项无详情：{'、'.join(dead[:4])}",
                     fix="为榜单项补 catalog 详情或从榜单移除该项（死链）")
    refs = {r.get("ref_id") for r in ranks}
    orphans = [k for k in list(poi_cat) + list(food_cat) if k not in refs]
    if orphans:
        return Check("R11", "榜单无死链", WARN, f"{len(ranks)} 榜项均有详情；{len(orphans)} 个详情无对应榜单项",
                     "详情库多出（可能榜单已截断），不算死链")
    return Check("R11", "榜单无死链", PASS, f"{len(ranks)} 个榜单项与详情一一对应", "无死链")


def _r12_triple_consistency(trip_plan: dict | None = None) -> Check:
    """R12（§6.9/宪法第 4 条）：同一事实（票价）在行程槽位与 catalog 详情取值一致；无则 skip。

    预算模块退役后本规则由“三处同源”收敛为“行程 ↔ 详情”两处同源。"""
    if not trip_plan:
        return Check("R12", "两处同源", SKIP, "未传 trip_plan",
                     "行程/详情尚未共享同一 catalog，本次不判定；接入后自动生效")
    cat = (trip_plan.get("catalog") or {}).get("poi") or {}
    mismatches = []
    n_blocks = 0
    for day in trip_plan.get("itinerary") or []:
        for b in day.get("blocks") or []:
            n_blocks += 1
            det = cat.get(str(b.get("spot") or ""))
            if not det:
                continue
            if b.get("ticket_price") != det.get("ticket_price"):
                mismatches.append(f"{b.get('spot')}（行程 {b.get('ticket_price')} vs 详情 {det.get('ticket_price')}）")
    if mismatches:
        return Check("R12", "两处同源", FAIL, f"{len(mismatches)} 处票价不一致：{'；'.join(mismatches[:3])}",
                     fix="行程槽位与详情卡均读同一 catalog 票价值，不得任一处单独改写或另起口径")
    return Check("R12", "两处同源", PASS,
                 f"行程/详情同一票价事实一致（{n_blocks} 槽）", "同源")


def run_quality_gate(*, decisions: list[SpotDecision], plan: dict, profiles: dict,
                     days: int,
                     pitfall: list[dict] | None = None,
                     food_profiles: dict | None = None,
                     day_weekdays: list[str] | None = None,
                     today: str | None = None,
                     locs: dict | None = None, top_n: int = TOP_N_HEAT,
                     legs: list[dict] | None = None,
                     trip_plan: dict | None = None) -> QualityReport:
    """对一份规划跑完整门禁，产出 QualityReport。纯函数、只读、不调 LLM。

    入参全部来自 service.trip 在'生成规划'后已有的数据（profiles/plan/heat_rows/
    pitfall/sources）与 build_decisions 的统一对象；缺什么对应规则就 skip。"""
    decisions = decisions or []
    plan = plan or {}
    profiles = profiles or {}
    checks = [
        _r1_coverage(plan, profiles, days),
        _r2_no_silent_drop(decisions, top_n),
        _r3_time_slot(plan, profiles, decisions, day_weekdays),
        _r4_time_feasible(plan, profiles, legs),
        _r5_route(plan, locs),
        _r7_food(plan, food_profiles),
        _r8_pitfall_attribution(pitfall, decisions),
        _r9_timeliness(decisions, today),
        _r10_sources(decisions),
        _r11_no_dead_link(trip_plan),
        _r12_triple_consistency(trip_plan),
    ]
    return QualityReport(checks=checks)


def apply_trip_plan_checks(report: QualityReport, trip_plan: dict | None) -> QualityReport:
    """定稿后用唯一 TripPlan 补判 R11/R12（输出完整性，不参与回炉循环），按 rule_id 就地替换。

    service 在 finalize 之后调用：把两条 skip 接口位换成真实结果，保留 repair_rounds；
    R11/R12 由构造不 fail（榜单 ref 只取 catalog 内同名、行程价回填自 catalog），但 fail 仍会同步进 unresolved。"""
    for c in (_r11_no_dead_link(trip_plan), _r12_triple_consistency(trip_plan)):
        for i, old in enumerate(report.checks):
            if old.rule_id == c.rule_id:
                report.checks[i] = c
                break
        else:
            report.checks.append(c)
    report.unresolved = [f"[{x.rule_id} {x.name}] {x.note}" for x in report.checks
                         if x.status == FAIL]
    return report
