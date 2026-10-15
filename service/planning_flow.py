"""图运行与独立finalizer。恢复只读冻结资料，不进入采集或重置浏览器会话。"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import datetime

from config import REPORT_DIR
from core import knowledge, rag
from core.evidence import VERSION as EVIDENCE_VERSION
from core.planning_runs import BudgetExhausted, RunCancelled, RunError, default_store
from pipeline.decision import build_decisions, build_trip_plan
from pipeline.planner import build_legs, build_overview
from pipeline.planning_policy import restore_quality
from pipeline.qc import Check, apply_trip_plan_checks
from pipeline.trip_render import render_html, render_markdown


def _decisions(s, plan):
    return build_decisions(
        candidates=s.get("cands"), verify_results=s.get("verify_results"), profiles=s["profiles"],
        food_profiles=s.get("food_profiles"),
        points_by_spot={**s.get("spot_points", {}), **s.get("food_points", {})},
        sources_by_spot={**s.get("spot_sources", {}), **s.get("food_sources", {})},
        heat_rows=s.get("heat_rows"), locs=s.get("locs"), travel_lines=s.get("travel_lines"),
        official_facts=s.get("official_map"), plan=plan)


def build_result(store, rid, output):
    """仅依赖持久快照与图产物，无网络、无旧线程局部变量。"""
    row = store.get(rid)
    s = row["snapshot"]
    best, plan = output["best"], output["best"]["plan"]
    report = restore_quality(best["quality"])
    constraints = best["constraints"]
    if row["policy"] == "hard-first-v1":
        report.checks.append(Check("H01", "明确硬约束", "fail" if constraints["hard_ids"] else "pass",
                                   str(len(constraints["hard_ids"])) + "项违反",
                                   "；".join(x["message"] for x in constraints["issues"])))
    decisions = _decisions(s, plan)
    legs = build_legs(s["city"], s["hotel"], plan, s.get("locs") or {}, s.get("travel_lines") or [])
    pitfall, foods = s.get("pitfall") or [], s.get("food_profiles") or {}
    snap = {"evidence_version": EVIDENCE_VERSION, "user_spots": s.get("user_spots"),
            "overview": build_overview(s["days"], plan, s["profiles"], pitfall, foods or None),
            "pitfall": pitfall, "digests": s.get("digests") or {}, "legs": legs,
            "geo_on": s.get("geo_on", False), "summary_note": plan.get("summary_note", ""),
            "guide_note": s.get("guide_note") or "",
            "dedupe_note": ("已剔除重复排入的点位：" + "、".join(plan["duplicate_drops"]) +
                            "（每个景点全程只排一次）") if plan.get("duplicate_drops") else "",
            "rebalance_note": ("已均衡排布：" + "；".join(plan["moved"]) +
                               "（避免某些天排得太空/太满）") if plan.get("moved") else "",
            "profiles": s["profiles"], "foods": foods, "heat": s.get("heat_rows") or []}
    meta = {"city": s["city"], "days": s["days"], "stay": s["hotel"],
            "prefs": s.get("preferences") or "", "preference_mode": s.get("preference_mode") or "均衡",
            "start_date": s.get("start_date"), "collect_mode": s["collect_mode"],
            "guide_videos": (s.get("guide") or {}).get("videos") or 0,
            "draft_days": len((s.get("draft_plan") or {}).get("days") or []),
            "generated_at": s["generated_at"], "facts_cutoff": s["facts_cutoff"], "synthetic": False,
            "planning_run_id": rid, "planning_policy": row["policy"],
            "planning_stop_reason": output["stop_reason"], "planning_usage": store.summary(rid),
            "constraint_coverage": constraints}
    trip_plan = build_trip_plan(meta=meta, decisions=decisions, plan=plan, quality=report, legs=legs,
                                snap=snap, food_min_samples=s.get("food_min_samples", 3), guide=s.get("guide"))
    apply_trip_plan_checks(report, trip_plan.to_dict())
    for message in s.get("funnel_unresolved") or []:
        entry = "[漏斗] " + message
        if entry not in report.unresolved:
            report.unresolved.append(entry)
    if row["policy"] == "hard-first-v1" and constraints["unknown_ids"]:
        report.unresolved.append("[约束核实] 部分点位缺少有效开放时间或出发日闭馆资料，未判定不代表满足约束")
    quality = report.to_dict() | {"constraint_coverage": constraints, "strategy_version": row["policy"]}
    trip_plan.quality = quality
    tp = trip_plan.to_dict()
    # run后缀避免同一秒同城任务相互覆盖；过滤Windows路径字符。
    city = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s["city"])[:60]
    stamp = datetime.fromisoformat(s["generated_at"]).strftime("%Y%m%d_%H%M%S")
    name = f"行程_{city}_{stamp}_{rid[:8]}.md"
    return {"report_path": str(REPORT_DIR / name), "report_name": name, "html_name": None,
            "markdown": render_markdown(tp), "cache_hit": False, "draft_plan": s.get("draft_plan"),
            "pitfall_digest": pitfall, "heat_rank": s.get("heat_rows") or [], "quality": quality,
            "decision_table": [d.to_row() for d in decisions], "trip_plan": tp,
            "funnel": {**s.get("funnel_info", {}), "notices": list(s.get("funnel_unresolved") or [])},
            "stats": {"spots": len(s.get("spot_points") or {}), "foods": len(s.get("food_points") or {}),
                      "days": s["days"], "points": s.get("point_count", 0),
                      "elapsed": round(s.get("elapsed_before_planning", 0) + time.time() - row["created_at"], 1),
                      "elapsed_includes_resume_wait": True},
            "planning_run_id": rid, "planning_usage": store.summary(rid),
            "planning_stop_reason": output["stop_reason"]}


def _write(path, text):
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RunError("报告文件已被外部修改，拒绝覆盖；请保留原稿后另建版本")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    temporary.replace(path)


def finalize_run(store, rid, output, *, hook=None, log=lambda message: None):
    """调用方持有lease；各外部写出幂等，部分失败持久标记可补做。"""
    store.guard(rid)
    if not output.get("complete"):
        raise RunError("规划子图尚未完成，不能伪造最终报告")
    row = store.get(rid)
    result = store.step(rid, "artifact_payload")
    if result is None:
        result = build_result(store, rid, output)
        store.put_step(rid, "artifact_payload", result)
    progress = store.step(rid, "postprocessing") or {}

    def checkpoint(name, value):
        progress[name] = value
        store.put_step(rid, "postprocessing", progress)
        if hook:
            hook(name, result)

    from pathlib import Path
    report_path = Path(result["report_path"])
    if report_path.parent.resolve() != REPORT_DIR.resolve():
        raise RunError("报告输出根目录变更，请进行显式迁移")
    if progress.get("report_written") != "done":
        store.guard(rid)
        _write(report_path, result["markdown"])
        checkpoint("report_written", "done")
    if progress.get("html_written") != "done":
        store.guard(rid)
        try:
            html_path = report_path.with_suffix(".html")
            _write(html_path, render_html(result["trip_plan"]))
            result["html_name"] = html_path.name
            checkpoint("html_written", "done")
        except Exception as exc:
            checkpoint("html_written", "failed:" + type(exc).__name__)
            log("HTML生成失败，Markdown保留；可按同run补做")
    if progress.get("html_written") == "done":
        result["html_name"] = report_path.with_suffix(".html").name
    if progress.get("report_registered") != "done":
        store.guard(rid)
        s = row["snapshot"]
        knowledge.register_report(f"行程·{s['city']}{s['days']}天", str(report_path),
                                  video_count=s.get("video_count", 0), comment_count=s.get("comment_count", 0))
        checkpoint("report_registered", "done")
    if progress.get("index_written") != "done":
        store.guard(rid)
        try:
            if not rag.available():
                checkpoint("index_written", "skipped:no_embedding")
            else:
                outcome = rag.index_result_complete(result)
                checkpoint("index_written", "done" if outcome["complete"] else "failed:partial_index")
        except Exception as exc:
            checkpoint("index_written", "failed:" + type(exc).__name__)
            log("检索索引未完成，报告可用，已保留补做状态")
    result["postprocessing"] = dict(progress)
    store.guard(rid)
    store.put_step(rid, "artifact_payload", result)
    store.update(rid, result=result, status="done", stop_reason=output["stop_reason"])
    log(f"规划完成：{report_path.name}；实际模型请求 {store.summary(rid)['http_attempts']} 次（planning范围）")
    return result


def run_new(snapshot, job, *, policy="hard-first-v1", max_attempts=12, ttl_seconds=86400, log=print):
    from pipeline.planning_graph import execute
    store = default_store()
    rid = store.create(snapshot, job_id=job["id"], policy=policy, max_attempts=max_attempts,
                       ttl_seconds=ttl_seconds)
    job["planning_run_id"] = rid
    job["stage"] = "规划与质量检查"
    knowledge.record_job(job)
    if job.get("cancel_requested"):
        store.cancel(rid)
    with store.lease(rid):
        output = execute(store, rid)
        job["stage"] = "渲染路书"
        return finalize_run(store, rid, output, log=log)


def _worker(store, rid, lease):
    from pipeline.planning_graph import execute
    from service.research import JOBS
    row = store.get(rid)
    jid = row["job_id"]
    job = JOBS[jid]
    try:
        result = finalize_run(store, rid, execute(store, rid),
                              log=lambda message: job["log"].append(message))
        job.update(status="done", stage="完成", result=result, error=None)
    except RunCancelled:
        store.update(rid, status="cancelled", stop_reason="cancel_requested")
        job.update(status="cancelled", stage="已取消", error=None)
    except Exception as exc:
        if store.get(rid)["status"] not in {"cancelled", "budget_exhausted", "fatal", "invalid_plan"}:
            store.update(rid, status="failed", stop_reason=type(exc).__name__)
        job.update(status="error", stage="规划停止", error=str(exc))
    finally:
        try:
            knowledge.record_job(job)
        except Exception as exc:
            job["log"].append("任务登记未完成：" + type(exc).__name__)
        finally:
            lease.__exit__(None, None, None)


def resume(rid):
    from service.research import JOBS, _LOCK
    store = default_store()
    row = store.validate(rid)
    view = store.public(rid)
    if not view["resumable"]:
        raise RunError("；".join(view["resume_blockers"]))
    lease = store.lease(rid)
    lease.__enter__()  # 派线程前抢锁，两个请求不会双重派发。
    try:
        jid = row["job_id"]
        if not jid:
            raise RunError("运行未关联业务job，不能由API恢复")
        with _LOCK:
            JOBS[jid] = {"id": jid, "kind": "trip", "keyword": f"{row['snapshot']['city']}行程",
                         "mode": "trip", "status": "running", "stage": "恢复规划",
                         "log": ["复用已冻结资料恢复规划，不重新采集"], "result": row["result"],
                         "error": None, "planning_run_id": rid, "cancel_requested": False,
                         "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(timespec="seconds")}
        store.update(rid, status="running")
        threading.Thread(target=_worker, args=(store, rid, lease), daemon=True).start()
    except Exception:
        lease.__exit__(None, None, None)
        raise
    return {"run_id": rid, "job_id": jid, "status": "accepted"}


def job_state(job_id, original=None):
    """将持久规划状态接回既有任务轮询接口。"""
    from service.research import JOBS
    store = default_store()
    row = store.for_job(job_id)
    if row is None:
        return original
    view = store.public(row["run_id"])
    if job_id in JOBS and JOBS[job_id].get("status") == "running":
        job = dict(JOBS[job_id])
        job["stage"] = {"generate": "生成行程", "normalize": "校验规划结构",
                        "coverage_repair": "补齐安排", "select_coverage": "比较候选安排",
                        "finish_candidate": "整理规划", "quality": "检查行程约束",
                        "compare": "比较规划质量", "begin_outer": "准备调整规划",
                        "finish": "规划定稿"}.get(view["node"], job.get("stage", "规划中"))
    else:
        status = "done" if row["status"] == "done" else "cancelled" if row["cancel_requested"] else "error"
        job = dict(original or {}) | {"id": job_id, "kind": "trip", "mode": "trip", "status": status,
                                      "stage": "完成" if status == "done" else "规划已停止，可检查恢复状态",
                                      "result": row["result"], "error": row["stop_reason"], "log": []}
    return job | {"planning_run_id": row["run_id"], "planning": view}


def ensure_runtime():
    try:
        from pipeline.planning_graph import execute
    except ImportError as exc:
        raise RuntimeError("规划依赖未安装或不兼容，请在项目环境运行 pip install -r requirements.txt") from exc
