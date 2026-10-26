"""任务服务：API 只在这里提交/查询/取消，不直接执行长流程。

task_jobs 是权威状态；JOBS 只在嵌入 Worker 时作为同步给旧接口的缓存视图。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

from config import AUTH_MODE, LOCAL_OWNER_ID, WORKER_MAX_ATTEMPTS
from core.storage import report_store, task_store
from core.storage.tasks import (CANCELLED, FAILED, NEEDS_REVIEW, QUEUED, RETRY_WAIT,
                                RUNNING, SUCCEEDED, TaskNotFound)

_TERMINAL = (SUCCEEDED, FAILED, CANCELLED)


def _store():
    return task_store()


def _reports():
    return report_store()


def submit(kind: str, payload: dict, *, owner_id: str, idempotency_key: str | None = None,
           queue_name: str = "pipeline", max_attempts: int = WORKER_MAX_ATTEMPTS,
           parent_version_id: str | None = None,
           base_report_id: str | None = None) -> dict:
    return _store().submit(owner_id=owner_id, idempotency_key=idempotency_key or uuid.uuid4().hex,
                           kind=kind, queue_name=queue_name, payload=payload,
                           max_attempts=max_attempts,
                           parent_version_id=parent_version_id,
                           base_report_id=base_report_id)


def submit_trip(*, owner_id: str, city: str, days: int, hotel: str = "",
                spots: list[str] | None = None, preferences: str = "",
                preference_mode: str = "均衡", start_date: str | None = None,
                excluded_spots: list[str] | None = None,
                idempotency_key: str | None = None) -> dict:
    payload = {"city": city, "days": days, "hotel": hotel, "spots": spots,
               "preferences": preferences, "preference_mode": preference_mode,
               "start_date": start_date, "excluded_spots": excluded_spots}
    return submit("trip", payload, owner_id=owner_id,
                  idempotency_key=idempotency_key, queue_name="pipeline")


def submit_research(*, owner_id: str, keyword: str, mode: str = "standard",
                    force: bool = False, idempotency_key: str | None = None) -> dict:
    return submit("research", {"keyword": keyword, "mode": mode, "force": force},
                  owner_id=owner_id, idempotency_key=idempotency_key,
                  queue_name="pipeline")


def submit_heat(*, owner_id: str, city: str,
                idempotency_key: str | None = None) -> dict:
    return submit("heat", {"city": city}, owner_id=owner_id,
                  idempotency_key=idempotency_key, queue_name="pipeline")


def submit_agent_question(*, owner_id: str, question: str, job_id: str | None = None,
                          report_name: str | None = None,
                          history: list[dict] | None = None,
                          idempotency_key: str | None = None) -> dict:
    return submit("agent_ask", {"question": question, "job_id": job_id,
                                "report_name": report_name, "history": history or []},
                  owner_id=owner_id, idempotency_key=idempotency_key,
                  queue_name="query", max_attempts=2)


def _legacy_visible(owner_id: str) -> bool:
    return AUTH_MODE == "local_single_user" and owner_id == LOCAL_OWNER_ID


def _legacy_job(job_id: str, owner_id: str) -> dict | None:
    if not _legacy_visible(owner_id):
        return None
    try:
        from service import research
        return research.get_job(job_id)
    except Exception:
        return None


def _event_log(job_id: str, limit: int = 200) -> list[str]:
    try:
        events = _store().events(job_id)
    except Exception:
        return []
    logs = [str((e.get("payload") or {}).get("message") or "")
            for e in events if e.get("event_type") == "log"]
    return [x for x in logs if x][-limit:]


def _attach_version_view(view: dict, job: dict) -> dict:
    try:
        version = _reports().find_version_by_job(job["id"])
    except Exception:
        version = None
    if version:
        view["report_id"] = version["report_id"]
        view["version_id"] = version["version_id"]
        view["version_no"] = version["version_no"]
        view["artifact_state"] = version["artifact_state"]
        view["index_state"] = version["index_state"]
        try:
            report = _reports().get_report(version["report_id"], job["owner_id"])
            view["active"] = report.get("active_version_id") == version["version_id"]
            view["active_version_id"] = report.get("active_version_id")
        except Exception:
            pass
    return view


def get_job_view(job_id: str, owner_id: str | None = None) -> dict | None:
    store = _store()
    try:
        job = store.get(job_id, owner_id)
    except TaskNotFound:
        job = None
    if job is None:
        legacy = _legacy_job(job_id, owner_id or LOCAL_OWNER_ID)
        if legacy is None:
            return None
        legacy = dict(legacy)
        legacy.setdefault("state", "done" if legacy.get("status") == "done"
                          else "cancelled" if legacy.get("status") == "cancelled"
                          else "failed" if legacy.get("status") == "error" else "running")
        legacy["legacy"] = True
        return legacy

    # 运行中优先显示旧的实时日志；终态以持久结果为准。
    legacy = {}
    if job["state"] not in _TERMINAL:
        try:
            from service.research import JOBS, _LOCK
            with _LOCK:
                raw = JOBS.get(job_id)
                legacy = dict(raw) if raw else {}
        except Exception:
            legacy = {}
    result = job.get("result_json")
    view = {
        "id": job["id"], "job_id": job["id"], "kind": job["kind"],
        "keyword": (job.get("payload") or {}).get("keyword")
        or (f"{job['payload'].get('city', '')} {job['payload'].get('days', '')}天行程"
            if job["kind"] in ("trip", "trip_change") else
            f"{job['payload'].get('city', '')} 热度榜" if job["kind"] == "heat"
            else job["payload"].get("question") or "任务"),
        "mode": job["kind"], "status": job.get("legacy_status", "running"),
        "state": job["state"], "stage": legacy.get("stage") or job.get("stage") or job["state"],
        "log": legacy.get("log") or _event_log(job_id),
        "result": result, "error": job.get("error_detail") or legacy.get("error"),
        "cancel_requested": job.get("cancel_requested", False),
        "owner_id": job["owner_id"], "trace_id": job["trace_id"],
        "queue": job["queue_name"], "attempt": job["attempt"],
        "max_attempts": job["max_attempts"], "quality_status": job.get("quality_status"),
        "created_at": datetime.fromtimestamp(job["created_at"]).isoformat(timespec="seconds"),
        "finished_at": (datetime.fromtimestamp(job["finished_at"]).isoformat(timespec="seconds")
                        if job.get("finished_at") else None),
        "result_ref": job.get("result_ref"),
    }
    if job["state"] == QUEUED:
        view["stage"] = "排队中（等待 Worker 领取）"
    elif job["state"] == RETRY_WAIT:
        view["stage"] = "等待重试"
    elif job["state"] == NEEDS_REVIEW:
        view["stage"] = "待人工复核（不会自动重放外部请求）"
        view["retryable"] = job["attempt"] < job["max_attempts"]
        view["status"] = "error"
        view["error"] = view["error"] or "外部调用状态不确定或任务失败，需人工处理后重试"
    elif job["state"] == RUNNING:
        view["status"] = "running"
    elif job["state"] == CANCELLED:
        view["status"] = "cancelled"

    # 规划子图状态接回旧轮询接口。
    try:
        from service import planning_flow
        planning = planning_flow.job_state(job_id)
    except Exception:
        planning = None
    if planning:
        view["planning_run_id"] = planning.get("planning_run_id")
        view["planning"] = planning.get("planning")
        if not view["result"] and planning.get("result"):
            view["result"] = planning["result"]
    if job["state"] == SUCCEEDED and result and job["kind"] in ("trip", "trip_change"):
        try:
            v = _reports().find_version_by_job(job["id"])
            if v is None:
                ensure_version_for_job(job)
        except Exception:
            pass
    view = _attach_version_view(view, job)
    return view


def get_events(job_id: str, owner_id: str, after: int = 0) -> list[dict]:
    job = _store().get(job_id, owner_id)
    return _store().events(job_id, after=after, owner_id=owner_id)


def cancel_job(job_id: str, owner_id: str) -> dict:
    store = _store()
    try:
        job = store.get(job_id, owner_id)
    except TaskNotFound:
        job = None
    if job is not None:
        out = store.cancel(job_id, owner_id)
        try:
            from service import research
            research.cancel_job(job_id)
        except Exception:
            pass
        try:
            from core.planning_runs import default_store, RunError
            from service import planning_flow
            p = default_store().for_job(job_id)
            if p and p["status"] not in ("done", "cancelled"):
                default_store().cancel(p["run_id"])
        except Exception:
            pass
        return out
    # 旧任务（升级前提交）兼容
    if not _legacy_visible(owner_id):
        raise TaskNotFound(job_id)
    from service import research
    if research.get_job(job_id) is None:
        raise TaskNotFound(job_id)
    if not research.cancel_job(job_id):
        raise TaskNotFound("任务不在运行中")
    return {"id": job_id, "state": CANCELLED, "legacy_status": "cancelled"}


def retry_job(job_id: str, owner_id: str) -> dict:
    return _store().retry(job_id, owner_id)


def ensure_version_for_job(job: dict, result: dict | None = None) -> dict | None:
    """为已完成任务登记不可变版本（幂等）。"""
    result = result or job.get("result_json") or {}
    if (not isinstance(result, dict) or not result.get("report_name")
            or not result.get("trip_plan")):
        return None
    reports = _reports()
    try:
        old = reports.find_version_by_job(job["id"])
    except Exception:
        old = None
    if old is not None:
        return old
    payload = job.get("payload") or {}
    try:
        if job["kind"] == "trip_change" and payload.get("report_id") and payload.get("base_version_id"):
            version = reports.register_result(
                job_id=job["id"], owner_id=job["owner_id"], result=result,
                report_id=payload["report_id"],
                parent_version_id=payload["base_version_id"],
                change_summary=str(payload.get("change_summary") or ""),
                activate=False, request=payload)
        else:
            version = reports.register_result(
                job_id=job["id"], owner_id=job["owner_id"], result=result,
                activate=True, request=payload)
    except Exception:
        # 并发登记（Worker 与查询侧同时触发）时以唯一 job_id 为准，复用已存在版本。
        version = reports.find_version_by_job(job["id"])
        if version is None:
            raise
    try:
        _store().attach_result_meta(job["id"], {
            "report_id": version["report_id"], "version_id": version["version_id"],
            "version_no": version["version_no"], "quality_status": version["quality_status"],
        })
    except Exception:
        pass
    return version


def list_job_views(owner_id: str, limit: int = 50) -> list[dict]:
    out = []
    for job in _store().list_jobs(owner_id, limit=limit):
        payload = job.get("payload") or {}
        keyword = payload.get("keyword")
        if not keyword:
            if job["kind"] in ("trip", "trip_change"):
                keyword = f"{payload.get('city', '')} {payload.get('days', '')}天行程"
            elif job["kind"] == "heat":
                keyword = f"{payload.get('city', '')} 热度榜"
            else:
                keyword = payload.get("question") or "任务"
        out.append({
            "id": job["id"], "kind": job["kind"], "keyword": keyword,
            "mode": payload.get("mode") or job["kind"],
            "status": job.get("legacy_status", "running"), "state": job["state"],
            "stage": job.get("stage"), "error": job.get("error_detail"),
            "created_at": datetime.fromtimestamp(job["created_at"]).isoformat(timespec="seconds"),
            "finished_at": (datetime.fromtimestamp(job["finished_at"]).isoformat(timespec="seconds")
                            if job.get("finished_at") else None),
            "trace_id": job["trace_id"], "quality_status": job.get("quality_status"),
        })
    return out


def queue_stats() -> dict:
    return _store().stats()