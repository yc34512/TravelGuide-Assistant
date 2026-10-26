"""行程变更、不可变版本、确定性 diff 与激活 CAS。

修改行程是显式新任务：新输入、新运行、新预算。旧版不改写；新旧方案都按
更新后的约束重新检查后再比较。LLM 只能解释差异，差异由代码计算。
"""
from __future__ import annotations

from config import PLANNING_GRAPH_ENABLED, PLANNING_POLICY
from core.storage import report_store
from service import task_service

_ALLOWED_CHANGES = {"days", "hotel", "spots", "preferences", "preference_mode",
                    "start_date", "excluded_spots", "cleared_exclusions", "note"}
_MODES = ("省钱优先", "体验优先", "均衡")


class ChangeError(ValueError):
    pass


def _result(version: dict) -> dict:
    return (version or {}).get("result") or (version or {}).get("result_json") or {}


def _trip_plan(version: dict) -> dict:
    return _result(version).get("trip_plan") or {}


def _blocks(version: dict) -> list[dict]:
    out = []
    for day in _trip_plan(version).get("itinerary") or []:
        for block in day.get("blocks") or []:
            if isinstance(block, dict):
                out.append({"day": day.get("day"), "slot": block.get("slot"),
                            "spot": str(block.get("spot") or ""),
                            "transport": block.get("transport") or "",
                            "duration": block.get("duration") or ""})
    return out


def _spots(version: dict) -> list[str]:
    out = []
    for b in _blocks(version):
        if b["spot"] and b["spot"] not in out:
            out.append(b["spot"])
    return out


def base_request(version: dict) -> dict:
    """优先使用版本登记时保存的原始请求；旧版本退化为从结果反推并标明限制。"""
    req = ((version or {}).get("material_manifest") or {}).get("request") or {}
    if req:
        return {k: req.get(k) for k in ("city", "days", "hotel", "spots", "preferences",
                                        "preference_mode", "start_date", "excluded_spots")}
    meta = _trip_plan(version).get("meta") or {}
    return {
        "city": meta.get("city") or "",
        "days": int(meta.get("days") or len(_trip_plan(version).get("itinerary") or []) or 1),
        "hotel": meta.get("stay") or "",
        "spots": _spots(version),
        "preferences": meta.get("prefs") or "",
        "preference_mode": meta.get("preference_mode") or "均衡",
        "start_date": meta.get("start_date"),
        "excluded_spots": [],
        "_reconstructed": True,
    }


def _strings(value, field: str = "excluded_spots") -> list[str]:
    if not isinstance(value, list):
        raise ChangeError(f"{field} 必须是字符串数组")
    return [str(x).strip() for x in value if str(x).strip()]


def merge_changes(base: dict, changes: dict) -> tuple[dict, str]:
    if not isinstance(changes, dict) or not changes:
        raise ChangeError("changes 不能为空")
    if "city" in changes:
        raise ChangeError("更换城市属于新行程，请新建任务")
    unknown = set(changes) - _ALLOWED_CHANGES
    if unknown:
        raise ChangeError("不支持的变更字段：" + "、".join(sorted(unknown)))
    merged = dict(base)
    notes = []
    if "days" in changes:
        try:
            days = int(changes["days"])
        except (TypeError, ValueError):
            raise ChangeError("days 必须是 1~7 的整数")
        if not 1 <= days <= 7:
            raise ChangeError("days 必须是 1~7 的整数")
        if days != int(base.get("days") or 0):
            notes.append(f"天数 {base.get('days')}→{days}")
        merged["days"] = days
    if "hotel" in changes:
        merged["hotel"] = str(changes["hotel"] or "").strip()
        notes.append("住宿位置调整")
    if "preference_mode" in changes:
        mode = str(changes["preference_mode"] or "").strip()
        if mode not in _MODES:
            raise ChangeError("preference_mode 仅支持 省钱优先 / 体验优先 / 均衡")
        merged["preference_mode"] = mode
        notes.append(f"选点倾向改为{mode}")
    if "start_date" in changes:
        merged["start_date"] = str(changes["start_date"] or "").strip() or None
        notes.append("出发日期调整")
    if "spots" in changes:
        spots = changes["spots"]
        if spots is not None:
            spots = _strings(spots, "spots")
        merged["spots"] = spots
        notes.append("指定点位清单调整")
    if "preferences" in changes:
        merged["preferences"] = str(changes["preferences"] or "").strip()
        notes.append("偏好调整")
    if "excluded_spots" in changes:
        add = _strings(changes["excluded_spots"], "excluded_spots")
        current = [str(x) for x in (base.get("excluded_spots") or [])]
        merged["excluded_spots"] = list(dict.fromkeys(current + add))
        if add:
            notes.append("排除 " + "、".join(add))
    if "cleared_exclusions" in changes:
        remove = set(_strings(changes["cleared_exclusions"], "cleared_exclusions"))
        current = [str(x) for x in (merged.get("excluded_spots") or [])]
        merged["excluded_spots"] = [x for x in current if x not in remove]
        if remove:
            notes.append("取消排除 " + "、".join(sorted(remove)))
    if "note" in changes:
        note = str(changes["note"] or "").strip()
        if note:
            prefs = str(merged.get("preferences") or "").strip()
            merged["preferences"] = (prefs + "；变更：" + note) if prefs else note
            notes.append("说明：" + note[:80])
    if not notes:
        raise ChangeError("没有实际变更")
    return merged, "；".join(notes)


def submit_change(*, owner_id: str, report_id: str, base_version_id: str,
                  changes: dict, idempotency_key: str | None = None) -> dict:
    if not PLANNING_GRAPH_ENABLED or PLANNING_POLICY != "hard-first-v1":
        raise ChangeError("行程变更需要启用规划子图与 hard-first-v1 策略")
    store = report_store()
    version = store.get_version(base_version_id)
    if version.get("report_id") != report_id or version.get("owner_id") != owner_id:
        raise ChangeError("找不到该报告版本，或无权修改")
    if version.get("artifact_state") != "ready":
        raise ChangeError("该版本报告尚不可读，不能作为变更基线")
    if not _trip_plan(version):
        raise ChangeError("只有行程报告支持变更")
    base = base_request(version)
    merged, summary = merge_changes(base, changes)
    payload = {
        **{k: merged.get(k) for k in ("city", "days", "hotel", "spots", "preferences",
                                      "preference_mode", "start_date", "excluded_spots")},
        "report_id": report_id, "base_version_id": base_version_id,
        "change_summary": summary, "changes": changes, "base_request": base,
    }
    out = task_service.submit(
        "trip_change", payload, owner_id=owner_id, idempotency_key=idempotency_key,
        queue_name="pipeline", parent_version_id=base_version_id, base_report_id=report_id)
    out["change_summary"] = summary
    out["base_version_id"] = base_version_id
    return out


def run_change_job(job: dict, ctx) -> None:
    """pipeline Worker 内执行：走同一行程主流程，产出新报告文件。"""
    from service import trip
    payload = job.get("payload") or {}
    for key in ("report_id", "base_version_id"):
        if not payload.get(key):
            raise RuntimeError("变更任务缺少 " + key)
    trip._run_trip(
        job["id"], payload["city"], int(payload["days"]), payload.get("hotel", ""),
        payload.get("spots"), payload.get("preferences", ""),
        payload.get("preference_mode", "均衡"), payload.get("start_date"),
        payload.get("excluded_spots"))


def _weekdays(start_date, days: int) -> list[str]:
    if not start_date:
        return []
    from pipeline.planner import day_weekdays_from
    try:
        return day_weekdays_from(start_date, days) or []
    except Exception:
        return []


def recheck_under_constraints(version: dict, request: dict) -> dict:
    """按更新后的约束重新检查某一版方案（只做可由代码判定的硬约束）。"""
    blocks = _blocks(version)
    spots = {b["spot"] for b in blocks if b["spot"]}
    excluded = set(str(x) for x in (request.get("excluded_spots") or []))
    hard, issues = [], []
    conflicts = sorted(spots & excluded)
    for name in conflicts:
        hard.append(f"excluded:{name}")
        issues.append(f"用户明确排除{name}，旧方案仍排入")
    days = int(request.get("days") or 0)
    plan_days = len(_trip_plan(version).get("itinerary") or [])
    if days and plan_days != days:
        hard.append("contract:days")
        issues.append(f"方案为 {plan_days} 天，新需求为 {days} 天")
    weekdays = _weekdays(request.get("start_date"), days or plan_days)
    catalog = _trip_plan(version).get("catalog") or {}
    if weekdays:
        for b in blocks:
            det = (catalog.get("poi") or {}).get(b["spot"]) or {}
            close = str(det.get("close_day") or "")
            idx = int(b.get("day") or 1) - 1
            if close and 0 <= idx < len(weekdays) and weekdays[idx] in close:
                key = f"closed:{b['spot']}:{b['day']}"
                if key not in hard:
                    hard.append(key)
                    issues.append(f"{b['spot']} 在新出发日第{b['day']}天（{weekdays[idx]}）闭馆")
    return {"valid": not hard, "hard_ids": hard, "issues": issues,
            "coverage_note": ("旧版本未保存原始排除清单，已按本次可见约束检查"
                              if base_request(version).get("_reconstructed") else ""),
            "weekdays": weekdays}


def diff_versions(from_version: dict, to_version: dict) -> dict:
    a_blocks = {(b["day"], b["slot"]): b for b in _blocks(from_version)}
    b_blocks = {(b["day"], b["slot"]): b for b in _blocks(to_version)}
    a_spots, b_spots = _spots(from_version), _spots(to_version)
    added = [x for x in b_spots if x not in a_spots]
    removed = [x for x in a_spots if x not in b_spots]
    moved = []
    for spot in [x for x in a_spots if x in b_spots]:
        a_pos = [k for k, v in a_blocks.items() if v["spot"] == spot]
        b_pos = [k for k, v in b_blocks.items() if v["spot"] == spot]
        if a_pos != b_pos:
            moved.append({"spot": spot,
                          "from": [{"day": d, "slot": s} for d, s in a_pos],
                          "to": [{"day": d, "slot": s} for d, s in b_pos]})
    transport = []
    for key in sorted(set(a_blocks) & set(b_blocks), key=lambda x: (x[0] or 0, str(x[1]))):
        if a_blocks[key].get("transport") != b_blocks[key].get("transport"):
            transport.append({"day": key[0], "slot": key[1], "spot": b_blocks[key]["spot"],
                              "from": a_blocks[key].get("transport") or "",
                              "to": b_blocks[key].get("transport") or ""})
    a_ev = {e.get("evidence_id") for e in (from_version.get("evidence") or [])}
    b_ev = {e.get("evidence_id") for e in (to_version.get("evidence") or [])}
    a_req, b_req = base_request(from_version), base_request(to_version)
    constraint_changes = {}
    for key in ("days", "hotel", "preferences", "preference_mode", "start_date",
                "excluded_spots", "spots"):
        if a_req.get(key) != b_req.get(key):
            constraint_changes[key] = {"from": a_req.get(key), "to": b_req.get(key)}
    new_recheck_a = recheck_under_constraints(from_version, b_req)
    new_recheck_b = recheck_under_constraints(to_version, b_req)
    qa = (_result(from_version).get("quality") or {})
    qb = (_result(to_version).get("quality") or {})
    summary_bits = []
    if added:
        summary_bits.append("新增 " + "、".join(added))
    if removed:
        summary_bits.append("移除 " + "、".join(removed))
    if moved:
        summary_bits.append("调整时段 " + "、".join(x["spot"] for x in moved))
    if transport:
        summary_bits.append(f"交通变化 {len(transport)} 段")
    if constraint_changes:
        summary_bits.append("约束变化：" + "、".join(constraint_changes))
    return {
        "from_version_id": from_version.get("version_id"),
        "to_version_id": to_version.get("version_id"),
        "from_version_no": from_version.get("version_no"),
        "to_version_no": to_version.get("version_no"),
        "added_spots": added, "removed_spots": removed, "moved_spots": moved,
        "transport_changes": transport, "constraint_changes": constraint_changes,
        "evidence": {"added": sorted(x for x in b_ev - a_ev if x),
                     "removed": sorted(x for x in a_ev - b_ev if x)},
        "quality": {"from": {"score": qa.get("score"), "passed": qa.get("passed"),
                             "unresolved": qa.get("unresolved") or []},
                    "to": {"score": qb.get("score"), "passed": qb.get("passed"),
                           "unresolved": qb.get("unresolved") or []}},
        "recheck": {"from_under_new_constraints": new_recheck_a,
                    "to_under_new_constraints": new_recheck_b,
                    "compared_under": "new_constraints"},
        "summary": "；".join(summary_bits) or "两版结构无差异（以字段比较结果为准）",
    }


def version_list(report_id: str, owner_id: str) -> list[dict]:
    return report_store().list_versions(report_id, owner_id)


def get_report(report_id: str, owner_id: str) -> dict:
    return report_store().get_report(report_id, owner_id)


def get_version(version_id: str) -> dict:
    return report_store().get_version(version_id)


def activate(report_id: str, version_id: str, expected_active_version_id: str | None,
             owner_id: str) -> dict:
    return report_store().activate(report_id, version_id, expected_active_version_id, owner_id)


def diff_by_ids(from_version_id: str, to_version_id: str) -> dict:
    return diff_versions(get_version(from_version_id), get_version(to_version_id))