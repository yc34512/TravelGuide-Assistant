"""明确约束与软问题分别比较，不以软分抵消新的硬冲突。"""
from __future__ import annotations

from pipeline import qc
from pipeline.decision import normalize_official, official_expired
from pipeline.planner import ALLOWED_SLOTS


def assess_constraints(plan, snapshot):
    profiles = snapshot["profiles"]
    facts = snapshot.get("official_map") or {}
    weekdays = snapshot.get("day_weekdays") or []
    today = snapshot["today"]
    excluded = set(snapshot.get("constraints", {}).get("excluded_spots") or [])
    hard, issues, evaluated, unknown = set(), [], set(), set()

    def violation(key, note, *, name=None, day=None, slot=None):
        hard.add(key)
        kind = key.split(":", 1)[0]
        reference = (["snapshot", "constraints", "excluded_spots"] if kind == "excluded" else
                     ["snapshot", "official_map", name] if name else ["snapshot", "days"])
        issues.append({"id": key, "violation_id": key,
                       "constraint_id": f"{kind}:{name}" if name else key,
                       "kind": "hard", "severity": "hard", "message": note,
                       "object": name, "day": day, "slot": slot,
                       "evidence_refs": [{"path": reference}],
                       "judgement": "violated", "repair_hint": note})

    # 每个可判断的事实都记录覆盖，即使本次没有安排该点。资料冻结避免改计划时丢裁判资料。
    for name in profiles:
        fact = normalize_official(facts.get(name))
        expired = official_expired(fact, today)
        for kind, known in (("closed", bool(fact.close_day) and bool(weekdays)),
                            ("hours", qc._close_hour(fact.open_hours) is not None)):
            key = f"{kind}:{name}"
            (evaluated if known and not expired else unknown).add(key)
    for name in excluded:
        (evaluated if name in profiles else unknown).add(f"excluded:{name}")

    days = (plan or {}).get("days")
    valid = isinstance(days, list) and len(days) == snapshot["days"] and bool(days)
    seen_days, seen_spots = set(), set()
    for day in days if isinstance(days, list) else []:
        if not isinstance(day, dict) or type(day.get("day")) is not int:
            valid = False
            continue
        idx = day["day"] - 1
        if not 0 <= idx < snapshot["days"] or idx in seen_days:
            valid = False
        seen_days.add(idx)
        slots = day.get("slots")
        if not isinstance(slots, list) or not slots:
            valid = False
        seen_slots = set()
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                valid = False
                continue
            name, when = slot.get("spot"), slot.get("slot")
            if when not in ALLOWED_SLOTS or when in seen_slots or (when == "全天" and len(slots) != 1):
                valid = False
            seen_slots.add(when)
            if name not in profiles or name in seen_spots:
                valid = False
                continue
            seen_spots.add(name)
            if name in excluded:
                violation(f"excluded:{name}:{day['day']}", f"用户明确排除{name}，不能排入行程",
                          name=name, day=day["day"], slot=when)
            fact = normalize_official(facts.get(name))
            wd = weekdays[idx] if 0 <= idx < len(weekdays) else ""
            if f"closed:{name}" in evaluated and wd and fact.close_day in wd:
                violation(f"closed:{name}:{day['day']}", f"{name}在第{day['day']}天（{wd}）闭馆",
                          name=name, day=day["day"], slot=when)
            close = qc._close_hour(fact.open_hours)
            if (f"hours:{name}" in evaluated and when == "晚上" and
                    close is not None and close < qc.NIGHT_FEASIBLE_CLOSE_HOUR):
                violation(f"hours:{name}:{day['day']}:晚上", f"{name}官方闭园早于晚间，不能排晚上",
                          name=name, day=day["day"], slot=when)
    if not valid:
        violation("contract:plan", "规划结构、天数或点位不符合输入契约")
    return {"hard_ids": sorted(hard), "issues": issues, "evaluated_ids": sorted(evaluated),
            "unknown_ids": sorted(unknown), "contract_valid": valid}


def weighted(quality):
    return sum(2 if c["status"] == "fail" else 1 if c["status"] == "warn" else 0
               for c in quality["checks"])


def prefer(old, new, policy):
    if policy == "legacy-v1":
        return (weighted(new["quality"]) < weighted(old["quality"]), "legacy_problem_count")
    a, b = old["constraints"], new["constraints"]
    if not b["contract_valid"]:
        return False, "invalid_contract"
    if not set(a["evaluated_ids"]).issubset(b["evaluated_ids"]):
        return False, "lost_validation_coverage"
    if not a["contract_valid"]:
        # 尚无合法基线时，不能把空/坏结构的“少冲突”当作真实优势。
        return True, "first_valid_contract"
    old_ids, new_ids = set(a["hard_ids"]), set(b["hard_ids"])
    if not new_ids.issubset(old_ids):
        return False, "new_hard_violation"
    if new_ids < old_ids:
        return True, "hard_set_reduced"
    if weighted(new["quality"]) < weighted(old["quality"]):
        return True, "soft_improved"
    return False, "no_strict_improvement"


def evaluate(plan, s, policy="legacy-v1"):
    from pipeline.decision import build_decisions
    from pipeline.planner import build_legs
    facts = s.get("official_map")
    if policy == "hard-first-v1":
        # 过期事实只保留为快照中的待核实资料，不能绕过hard检查由旧QC强制修改行程。
        facts = {name: fact for name, fact in (facts or {}).items()
                 if not official_expired(normalize_official(fact), s["today"])}
    decisions = build_decisions(
        candidates=s.get("cands"), verify_results=s.get("verify_results"), profiles=s["profiles"],
        food_profiles=s.get("food_profiles"),
        points_by_spot={**s.get("spot_points", {}), **s.get("food_points", {})},
        sources_by_spot={**s.get("spot_sources", {}), **s.get("food_sources", {})},
        heat_rows=s.get("heat_rows"), locs=s.get("locs"), travel_lines=s.get("travel_lines"),
        official_facts=facts, plan=plan)
    legs = build_legs(s["city"], s["hotel"], plan, s.get("locs") or {}, s.get("travel_lines") or [])
    report = qc.run_quality_gate(
        decisions=decisions, plan=plan, profiles=s["profiles"], days=s["days"],
        pitfall=s.get("pitfall"), food_profiles=s.get("food_profiles"),
        locs=s.get("locs"), today=s["today"], day_weekdays=s.get("day_weekdays"), legs=legs)
    return {"plan": plan, "quality": report.to_dict(), "constraints": assess_constraints(plan, s)}


def restore_quality(raw):
    return qc.QualityReport(checks=[qc.Check(**c) for c in raw["checks"]],
                            repair_rounds=raw.get("repair_rounds", 0),
                            unresolved=list(raw.get("unresolved") or []))
