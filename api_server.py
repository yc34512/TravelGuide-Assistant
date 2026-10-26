"""FastAPI 服务：网页界面 + 持久任务接口。

接口：
    GET  /                     网页界面
    GET  /api/health           健康检查 + 知识库概览
    POST /api/research         发起攻略研究任务（202 + job_id）
    POST /api/trip             发起行程规划任务（202 + job_id）
    POST /api/heat/refresh     发起城市热度刷榜任务（202 + job_id）
    POST /api/ask              追问：mode=report 同步回答；mode=agent 202 + job_id
    GET  /api/kb/search        知识库语义检索
    GET  /api/heat/{city}      查询城市实时热度榜
    GET  /api/jobs/{id}        查询任务状态/阶段/结果（持久状态）
    GET  /api/jobs/{id}/events 增量事件（after 游标）
    POST /api/jobs/{id}/cancel 幂等取消
    POST /api/jobs/{id}/retry  人工复核后重试
    GET  /api/jobs/history     历史任务摘要
    GET  /api/reports          历史报告列表
    GET  /api/reports/download 下载报告（防目录穿越 + 归属校验）

API 只接收任务，不执行长流程；执行由有界 Worker 完成（嵌入或独立进程）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import (AUTH_MODE, KB_TTL_DAYS, LOCAL_OWNER_ID, REPORT_DIR,
                    WORKER_EMBEDDED, WORKER_ENABLED)
from core import geo, knowledge
from core.auth import AuthError, Principal, authenticate
from core.storage.tasks import IdempotencyConflict, QueueFull, TaskError, TaskNotFound
from pipeline import ask as ask_engine
from pipeline import heat
from service import task_service


@asynccontextmanager
async def _lifespan(app):
    group = None
    if WORKER_ENABLED and WORKER_EMBEDDED:
        try:
            from service.worker import WorkerGroup
            group = WorkerGroup().start()
        except Exception as exc:   # 单例已被外部 Worker 占用等情况：明确提示，不假装启动
            print(f"[api] 嵌入 Worker 未启动：{type(exc).__name__}: {exc}")
    try:
        yield
    finally:
        if group is not None:
            group.stop()


app = FastAPI(
    title="旅游攻略助手",
    description=(
        "抖音 UGC 采集 + AI 攻略整合，来源可溯。\n\n"
        "面向 AI 智能体/自动化平台：本服务全部接口均为无状态 HTTP JSON，"
        "可直接按 OpenAPI 规范导入 Dify / Coze / GPTs Actions / n8n / LangChain 等。\n"
        "典型调用链：POST /api/research 发起 -> 每 20 秒 GET /api/jobs/{id} 轮询 "
        "-> status=done 时取 result.markdown 获得带来源引用的完整报告。"
    ),
    version="1.3.0",
    lifespan=_lifespan,
)

_WEB_DIR = Path(__file__).parent / "web"


class ResearchIn(BaseModel):
    keyword: str
    mode: str = "standard"  # fast / standard / deep
    force: bool = False


class TripIn(BaseModel):
    city: str
    days: int = 2
    hotel: str = ""
    spots: list[str] | None = None  # 指定景点清单；缺省时自动圈定（混合候选验证）
    preferences: str = ""
    preference_mode: str = "均衡"  # 省钱优先 / 体验优先 / 均衡
    start_date: str | None = None  # 出发日 YYYY-MM-DD
    excluded_spots: list[str] | None = None  # 用户显式提交的硬约束


class HeatRefreshIn(BaseModel):
    city: str


class AskIn(BaseModel):
    question: str
    job_id: str | None = None
    report_name: str | None = None
    history: list[dict] | None = None  # 最近几轮追问 [{q, a}]
    mode: str = "report"               # report（原同步快路径）/ agent（有界工具循环）


class ChangeIn(BaseModel):
    base_version_id: str
    changes: dict


class ActivateIn(BaseModel):
    version_id: str
    expected_active_version_id: str | None = None


def current_principal(authorization: str | None = Header(default=None)) -> Principal:
    try:
        return authenticate(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"身份服务不可用：{type(exc).__name__}")


def _enforce_rate(scope: str, identity: str, limit: int, window: int) -> None:
    from core.limits import rate_limit
    result = rate_limit(scope, identity, limit, window)
    if not result.get("allowed", True):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试",
                            headers={"Retry-After": str(window)})


def _submit_response(out: dict) -> JSONResponse:
    return JSONResponse(status_code=202, content={
        "job_id": out["id"], "request_id": out.get("request_id"),
        "state": out.get("state", "queued"), "status": out.get("legacy_status", "running"),
        "trace_id": out.get("trace_id"), "reused": bool(out.get("reused")),
    })


def _submit_error(exc: Exception) -> HTTPException:
    if isinstance(exc, IdempotencyConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, QueueFull):
        return HTTPException(status_code=503, detail=str(exc),
                             headers={"Retry-After": "30"})
    return HTTPException(status_code=503, detail=f"任务提交失败：{exc}")


def _report_job(body: AskIn, principal: Principal) -> dict | None:
    """按 job_id 或报告文件名取本人可读的报告结果。"""
    if body.job_id:
        view = task_service.get_job_view(body.job_id, principal.id)
        if view and view.get("result"):
            return view
    if body.job_id and principal.is_local:
        job = None
        from service import research
        job = research.JOBS.get(body.job_id)
        if not job or not job.get("result"):
            job = knowledge.load_job(body.job_id) or job
        if job and job.get("result"):
            return job
    if body.report_name:
        from core.storage import report_store
        version = report_store().find_by_report_name(body.report_name, principal.id)
        if version and version.get("result"):
            return {"id": version.get("job_id"), "keyword": body.report_name,
                    "result": version["result"]}
        if principal.is_local:
            job = knowledge.find_job_by_report(body.report_name)
            if job and job.get("result"):
                return job
    return None


@app.post("/api/ask", summary="就一份已生成的报告追问")
def ask_report(body: AskIn, principal: Principal = Depends(current_principal)):
    """mode=report（默认）保留原同步响应；mode=agent 返回 202 与 job_id。"""
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    if body.mode not in ("report", "agent"):
        raise HTTPException(status_code=400, detail="mode 仅支持 report / agent")
    job = _report_job(body, principal)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="找不到这份报告的任务档案，无法追问（可重新生成一份后再问）",
        )
    if body.mode == "agent":
        from core.storage import report_store
        version = None
        if body.report_name:
            version = report_store().find_by_report_name(body.report_name, principal.id)
        elif body.job_id:
            version = report_store().find_version_by_job(body.job_id)
            if version is not None and version.get("owner_id") != principal.id:
                version = None
        if version is None:
            raise HTTPException(
                status_code=409,
                detail="这份报告尚未登记可追溯版本，无法使用深度分析；请重新生成后再试")
        _enforce_rate("ask-agent", principal.id, 20, 60)
        try:
            out = task_service.submit_agent_question(
                owner_id=principal.id, question=question, job_id=body.job_id,
                report_name=body.report_name, history=body.history)
        except Exception as exc:
            raise _submit_error(exc)
        return _submit_response(out)
    try:
        text = ask_engine.answer(job["result"], question, body.history,
                                 owner_id=principal.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"追问失败：{e}")
    return {"answer": text, "job_id": job.get("id"), "keyword": job.get("keyword"),
            "mode": "report"}


@app.post("/api/heat/refresh", summary="发起城市热度刷榜（元数据轻量采集）")
def heat_refresh(body: HeatRefreshIn, principal: Principal = Depends(current_principal),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    city = body.city.strip()
    if not city:
        raise HTTPException(status_code=400, detail="城市不能为空")
    _enforce_rate("heat", principal.id, 10, 60)
    try:
        out = task_service.submit_heat(owner_id=principal.id, city=city,
                                       idempotency_key=idempotency_key)
    except Exception as exc:
        raise _submit_error(exc)
    return _submit_response(out)


@app.get("/api/heat/{city}", summary="查询城市热度榜（景点榜 + 美食榜）")
def city_heat(city: str):
    """返回该城榜单，按数据来源分三层兜底：快照 → 行程顺带 → 空榜引导。"""
    name = city.strip()
    rows = knowledge.load_heat_snapshots(name)
    source = source_at = source_label = ""
    trip = knowledge.find_latest_trip(name) if name else None

    if rows:
        kinds = {r.get("source") or "refresh" for r in rows}
        source_at = max((r.get("updated_at") or "") for r in rows)
        if trip:
            have = {r.get("spot") for r in rows}
            extra = [
                r for r in heat.rows_from_trip(trip["trip_plan"], trip["heat_rank"],
                                               trip.get("finished_at") or "")
                if r["spot"] not in have
            ]
            if extra:
                rows = sorted(rows + extra, key=lambda r: r.get("score", 0), reverse=True)
                kinds.add("trip")
                source_at = max(source_at, trip.get("finished_at") or "")
        source = ("trip" if kinds == {"trip"}
                  else "refresh" if kinds == {"refresh"} else "mixed")
        source_label = {
            "trip": "本次数据来自行程规划顺带采集（未单独刷榜）",
            "refresh": "本次数据来自刷榜采集（近 90 天抖音内容）",
            "mixed": "刷榜与行程规划采集混合的数据",
        }[source]
    elif trip:
        rows = heat.rows_from_trip(trip["trip_plan"], trip["heat_rank"],
                                   trip.get("finished_at") or "")
        source = "trip"
        source_at = trip.get("finished_at") or ""
        source_label = f"来自你 {source_at[:10]} 的一次《{name}》行程规划"

    ranking = [r for r in rows if r.get("kind") != "美食"]
    food_ranking = [r for r in rows if r.get("kind") == "美食"]
    return {
        "city": name, "ranking": ranking, "food_ranking": food_ranking,
        "source": source, "source_at": source_at, "source_label": source_label,
        "hint": "" if rows else (
            f"暂无《{name}》的榜单数据。两条路都行："
            "① 点「刷新榜单」跑一轮采集（只采元数据，约 3~5 分钟）；"
            "② 直接做一份行程规划——热度会一并采好并留在本页。"
        ),
    }


@app.get("/api/kb/search", summary="知识库语义检索（按问题检索历史档案片段）")
def kb_search(q: str, city: str = "", top_k: int = 0,
              principal: Principal = Depends(current_principal)):
    """语义检索本地点位知识；服务端按归属过滤，不接受客户端指定 owner。"""
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="查询不能为空")
    from core import rag
    hits = rag.search(query, top_k=(top_k or None), prefer_city=(city or "").strip())
    if not principal.is_local:
        from core.storage import report_store
        reports = report_store()
        owned = set()
        for name in {str((h.get("meta") or {}).get("ns") or "") for h in hits}:
            if not name:
                continue
            if reports.owner_of_report_name(name.replace("evidence-v1:", "")) == principal.id:
                owned.add(name)
        hits = [h for h in hits
                if (h.get("meta") or {}).get("source") == "city_guide"
                or (h.get("meta") or {}).get("ns") in owned]
    return {
        "query": query, "count": len(hits),
        "results": [
            {"text": h.get("text"), "score": h.get("score"),
             "spot": (h.get("meta") or {}).get("spot") or "",
             "city": (h.get("meta") or {}).get("city") or "",
             "kind": (h.get("meta") or {}).get("kind") or "",
             "source": (h.get("meta") or {}).get("source") or "",
             "report": (h.get("meta") or {}).get("ns") or ""}
            for h in hits
        ],
        "hint": "" if hits else (
            "未命中的两种可能：① 知识库还没有可检索语料（先生成一份行程报告）；"
            "② 未配置 embedding 服务（检索降级不影响报告与追问）。"
        ),
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_WEB_DIR / "index.html")


@app.get("/api/health", summary="健康检查 + 知识库概览")
def health():
    from core.cache import cache_status
    out = {"status": "ok", "auth_mode": AUTH_MODE, "cache": cache_status()}
    if AUTH_MODE == "local_single_user":
        out.update({"kb": knowledge.stats(), "kb_ttl_days": KB_TTL_DAYS,
                    "amap": geo.amap_usage(), "tasks": task_service.queue_stats()})
    return out


@app.post("/api/research", summary="发起攻略研究任务")
def start_research(body: ResearchIn, principal: Principal = Depends(current_principal),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    _enforce_rate("research", principal.id, 30, 60)
    try:
        out = task_service.submit_research(owner_id=principal.id, keyword=keyword,
                                           mode=body.mode, force=body.force,
                                           idempotency_key=idempotency_key)
    except Exception as exc:
        raise _submit_error(exc)
    return _submit_response(out)


@app.post("/api/trip", summary="发起行程规划任务（候选验证/调研/排行程）")
def start_trip_api(body: TripIn, principal: Principal = Depends(current_principal),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    city = body.city.strip()
    if not city:
        raise HTTPException(status_code=400, detail="城市不能为空")
    if not 1 <= body.days <= 7:
        raise HTTPException(status_code=400, detail="天数需在 1~7 之间")
    mode = body.preference_mode.strip() or "均衡"
    if mode not in ("省钱优先", "体验优先", "均衡"):
        raise HTTPException(status_code=400, detail="消费偏好仅支持：省钱优先 / 体验优先 / 均衡")
    excluded = [s.strip() for s in (body.excluded_spots or []) if str(s).strip()]
    if excluded:
        from config import PLANNING_GRAPH_ENABLED, PLANNING_POLICY
        if not PLANNING_GRAPH_ENABLED or PLANNING_POLICY != "hard-first-v1":
            raise HTTPException(status_code=400, detail="明确排除约束需要启用规划子图")
    _enforce_rate("trip", principal.id, 10, 60)
    try:
        out = task_service.submit_trip(
            owner_id=principal.id, city=city, days=body.days,
            hotel=body.hotel.strip(), spots=body.spots,
            preferences=body.preferences.strip(), preference_mode=mode,
            start_date=(body.start_date or "").strip() or None,
            excluded_spots=excluded or None, idempotency_key=idempotency_key)
    except Exception as exc:
        raise _submit_error(exc)
    return _submit_response(out)


@app.get("/api/jobs/history", summary="历史任务摘要（重启后仍可查）")
def jobs_history(principal: Principal = Depends(current_principal)):
    """历史任务摘要。注意：必须定义在 /api/jobs/{job_id} 之前。"""
    try:
        jobs = task_service.list_job_views(principal.id, limit=50)
    except Exception:
        jobs = []
    if principal.is_local:
        seen = {j["id"] for j in jobs}
        jobs += [j for j in knowledge.list_jobs() if j.get("id") not in seen]
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}", summary="查询任务状态/进度/结果")
def job_status(job_id: str, principal: Principal = Depends(current_principal)):
    job = task_service.get_job_view(job_id, principal.id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.get("legacy") and principal.is_local:
        from service import planning_flow
        job = planning_flow.job_state(job_id, job) or job
    return job


@app.get("/api/jobs/{job_id}/events", summary="增量任务事件（after 游标）")
def job_events(job_id: str, after: int = Query(default=0, ge=0),
               principal: Principal = Depends(current_principal)):
    try:
        events = task_service.get_events(job_id, principal.id, after=after)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job_id": job_id, "events": events,
            "next": events[-1]["event_id"] if events else after}


@app.post("/api/jobs/{job_id}/cancel", summary="取消运行中的任务")
def cancel_job(job_id: str, principal: Principal = Depends(current_principal)):
    """幂等取消。已终态返回 409；运行中记录 cancel_requested，由 Worker 协作式停止。"""
    job = task_service.get_job_view(job_id, principal.id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if (not job.get("legacy")
            and job.get("state") in ("succeeded", "failed", "cancelled")):
        raise HTTPException(status_code=409, detail="任务不在运行中，无法取消")
    try:
        out = task_service.cancel_job(job_id, principal.id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="任务不存在")
    except TaskError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "job_id": job_id, "state": out.get("state"),
            "status": out.get("legacy_status")}


@app.post("/api/jobs/{job_id}/retry", summary="人工复核后重试任务")
def retry_job(job_id: str, principal: Principal = Depends(current_principal)):
    try:
        out = task_service.retry_job(job_id, principal.id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="任务不存在")
    except TaskError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "job_id": job_id, "state": out.get("state"),
            "status": out.get("legacy_status")}


def _assert_planning_owner(run_id: str, principal: Principal) -> dict:
    from core.planning_runs import default_store
    store = default_store()
    row = store.get(run_id)   # KeyError/ValueError -> caller 404
    if principal.is_local:
        return row
    jid = row.get("job_id")
    if not jid or task_service.get_job_view(jid, principal.id) is None:
        raise HTTPException(status_code=404, detail="规划运行不存在")
    return row


@app.get("/api/planning-runs/{run_id}", summary="查询持久规划状态、恢复边界和调用账本")
def planning_status(run_id: str, principal: Principal = Depends(current_principal)):
    from core.planning_runs import default_store
    try:
        _assert_planning_owner(run_id, principal)
        return default_store().public(run_id)
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="规划运行不存在")


@app.get("/api/planning-runs/{run_id}/events", summary="读取规划节点、请求及采纳事件")
def planning_events(run_id: str, principal: Principal = Depends(current_principal)):
    from core.planning_runs import default_store
    try:
        _assert_planning_owner(run_id, principal)
        store = default_store()
        return {"run_id": run_id, "events": store.events(run_id)}
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="规划运行不存在")


@app.post("/api/planning-runs/{run_id}/resume", status_code=202,
          summary="按原快照恢复规划和补做报告；不重新采集")
def resume_planning(run_id: str, principal: Principal = Depends(current_principal)):
    from core.planning_runs import RunError
    from service import planning_flow
    try:
        _assert_planning_owner(run_id, principal)
        return planning_flow.resume(run_id)
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="规划运行不存在")
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/planning-runs/{run_id}/cancel", summary="持久取消规划；恢复不能清除取消状态")
def cancel_planning(run_id: str, principal: Principal = Depends(current_principal)):
    from core.planning_runs import default_store
    try:
        row = _assert_planning_owner(run_id, principal)
        store = default_store()
        if row["status"] == "done":
            raise HTTPException(status_code=409, detail="规划已完成")
        store.cancel(run_id)
        return {"ok": True, "run_id": run_id}
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="规划运行不存在")


@app.get("/api/reports", summary="历史报告列表")
def report_history(principal: Principal = Depends(current_principal)):
    reports = knowledge.list_history()
    from core.storage import report_store
    store = report_store()
    enriched = []
    for row in reports:
        name = row.get("report_path") or row.get("report_name") or row.get("name") or ""
        version = store.find_by_report_name(name)
        if version is None:
            if principal.is_local:
                enriched.append(row)
            continue
        if version.get("owner_id") != principal.id:
            continue
        active = False
        try:
            active = store.get_report(version["report_id"], principal.id).get(
                "active_version_id") == version["version_id"]
        except Exception:
            pass
        enriched.append({**row, "report_name": name, "report_id": version["report_id"],
                         "version_id": version["version_id"],
                         "version_no": version["version_no"], "active": active})
    return {"reports": enriched}


def _owned_report(report_id: str, principal: Principal) -> dict:
    from core.storage.reports import VersionNotFound
    from service import report_versions
    try:
        return report_versions.get_report(report_id, principal.id)
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="报告不存在")


@app.get("/api/reports/{report_id}/versions", summary="报告版本列表")
def report_versions_api(report_id: str, principal: Principal = Depends(current_principal)):
    from core.storage.reports import VersionNotFound
    from service import report_versions
    _owned_report(report_id, principal)
    try:
        versions = report_versions.version_list(report_id, principal.id)
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"report_id": report_id, "versions": versions}


@app.get("/api/reports/{report_id}/diff", summary="结构化版本差异（代码计算）")
def report_diff_api(report_id: str, from_version_id: str = "",
                    to_version_id: str = "",
                    principal: Principal = Depends(current_principal)):
    from core.storage.reports import VersionNotFound
    from service import report_versions
    report = _owned_report(report_id, principal)
    ids = {v["version_id"] for v in report_versions.version_list(report_id, principal.id)}
    from_id = from_version_id or report.get("active_version_id")
    to_id = to_version_id or from_id
    if from_id not in ids or to_id not in ids:
        raise HTTPException(status_code=404, detail="版本不存在")
    try:
        return report_versions.diff_by_ids(from_id, to_id)
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="版本不存在")


@app.post("/api/reports/{report_id}/activate", summary="激活一个可读版本（CAS）")
def report_activate_api(report_id: str, body: ActivateIn,
                        principal: Principal = Depends(current_principal)):
    from core.storage.reports import ReportError, VersionConflict, VersionNotFound
    from service import report_versions
    _owned_report(report_id, principal)
    try:
        out = report_versions.activate(report_id, body.version_id,
                                       body.expected_active_version_id, principal.id)
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="版本不存在")
    except ReportError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "report_id": report_id,
            "active_version_id": out.get("active_version_id")}


@app.post("/api/reports/{report_id}/changes", status_code=202,
          summary="基于某个版本创建行程变更任务")
def report_change_api(report_id: str, body: ChangeIn,
                      principal: Principal = Depends(current_principal),
                      idempotency_key: str | None = Header(default=None,
                                                           alias="Idempotency-Key")):
    from core.storage.reports import ReportError, VersionNotFound
    from service import report_versions
    _owned_report(report_id, principal)
    _enforce_rate("report-change", principal.id, 10, 60)
    try:
        out = report_versions.submit_change(
            owner_id=principal.id, report_id=report_id,
            base_version_id=body.base_version_id, changes=body.changes,
            idempotency_key=idempotency_key)
    except report_versions.ChangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="版本不存在")
    except ReportError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return JSONResponse(status_code=202, content={
        "job_id": out["id"], "request_id": out.get("request_id"),
        "state": out.get("state", "queued"), "status": out.get("legacy_status", "running"),
        "trace_id": out.get("trace_id"), "reused": bool(out.get("reused")),
        "base_version_id": out.get("base_version_id"),
        "change_summary": out.get("change_summary"),
    })


@app.get("/api/reports/{report_id}/summary", summary="报告与当前激活版本摘要")
def report_detail_api(report_id: str, principal: Principal = Depends(current_principal)):
    from service import report_versions
    report = _owned_report(report_id, principal)
    versions = report_versions.version_list(report_id, principal.id)
    return {"report_id": report_id, "owner_id": report.get("owner_id"),
            "active_version_id": report.get("active_version_id"),
            "versions": [{"version_id": v["version_id"], "version_no": v["version_no"],
                          "quality_status": v["quality_status"],
                          "artifact_state": v["artifact_state"], "created_at": v["created_at"]}
                         for v in versions]}


def _assert_report_owner(name: str, principal: Principal) -> None:
    if principal.is_local:
        return
    from core.storage import report_store
    owner = report_store().owner_of_report_name(name)
    if owner != principal.id:
        raise HTTPException(status_code=404, detail="报告不存在")


@app.get("/api/reports/download", summary="下载报告（Markdown/HTML，防目录穿越）")
@app.head("/api/reports/download", include_in_schema=False)
def download_report(name: str, principal: Principal = Depends(current_principal)):
    """按文件名下载报告。路径先 resolve 再校验父目录，防止 ../ 目录穿越。"""
    _assert_report_owner(name, principal)
    path = (REPORT_DIR / name).resolve()
    if path.parent != REPORT_DIR.resolve() or path.suffix not in (".md", ".html") or not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    media = "text/html" if path.suffix == ".html" else "text/markdown"
    disposition = "inline" if path.suffix == ".html" else "attachment"
    return FileResponse(path, filename=path.name, media_type=media,
                        content_disposition_type=disposition)