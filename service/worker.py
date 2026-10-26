"""有界任务 Worker：领取、心跳、进度同步、取消、重试与恢复。

API 只接收任务，不执行长流程。pipeline 队列首版容量 1，query 队列固定小容量；
执行器通过短事务领取任务，提交后才调用浏览器/模型/地图。JOBS 字典只作为
同步给旧 API 的缓存视图，task_jobs 才是权威状态。
"""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import (PIPELINE_WORKER_CAPACITY, QUERY_WORKER_CAPACITY,
                    WORKER_HEARTBEAT_SECONDS, WORKER_LEASE_SECONDS,
                    WORKER_MAX_ATTEMPTS, WORKER_POLL_SECONDS,
                    WORKER_PROGRESS_SECONDS, WORKER_RETRY_DELAY_SECONDS,
                    WORKER_ROLE)
from core.storage import task_store
from core.storage.reports import quality_status
from core.storage.tasks import (CANCELLED, FAILED, SUCCEEDED, SQLiteTaskStore,
                                TaskNotFound)


class JobCancelled(RuntimeError):
    pass


class NeedsReview(RuntimeError):
    """外部调用结果不确定或需要人工处理：不自动重放，等待明确操作。"""


class RetryableError(RuntimeError):
    """可证明未产生外部副作用、可安全重试的错误。"""


class JobContext:
    def __init__(self, store, job: dict, worker_id: str):
        self.store = store
        self.job = job
        self.job_id = job["id"]
        self.worker_id = worker_id
        self.generation = int(job["generation"])
        self.legacy: dict | None = None
        self._log_offset = 0
        self._last_stage = ""
        self._lost = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_heartbeat = 0.0
        self.cancel_requested = bool(job.get("cancel_requested"))

    def attach_legacy(self, legacy: dict) -> None:
        self.legacy = legacy

    def cancelled(self) -> bool:
        return self.cancel_requested or self._lost

    def stage(self) -> str:
        return (self.legacy or {}).get("stage") or self.job.get("stage") or ""

    def sync_progress(self, *, force: bool = False) -> bool:
        """把旧任务字典的 stage/log 同步到持久事件；返回是否仍持有租约。"""
        now = time.time()
        stage = None
        messages: list[str] = []
        offset = self._log_offset
        with self._lock:
            legacy = self.legacy
            if legacy is not None:
                stage = str(legacy.get("stage") or "")
                logs = list(legacy.get("log") or [])
                if len(logs) > self._log_offset:
                    messages = logs[self._log_offset:]
                    offset = self._log_offset
                    self._log_offset = len(logs)
                if legacy.get("cancel_requested"):
                    self.cancel_requested = True
        result = self.store.progress(self.job_id, self.worker_id, self.generation,
                                     stage=stage, log_messages=messages,
                                     log_offset=offset)
        if not result.get("owned"):
            self._lost = True
            if self.legacy is not None:
                self.legacy["cancel_requested"] = True
            return False
        if result.get("cancel_requested") or self.cancel_requested:
            self.cancel_requested = True
            if self.legacy is not None:
                self.legacy["cancel_requested"] = True
        if force or now - self._last_heartbeat >= WORKER_HEARTBEAT_SECONDS:
            ok = self.store.heartbeat(self.job_id, self.worker_id, self.generation,
                                      WORKER_LEASE_SECONDS)
            if not ok:
                self._lost = True
                if self.legacy is not None:
                    self.legacy["cancel_requested"] = True
                return False
            self._last_heartbeat = now
        return True

    def monitor(self) -> None:
        while not self._stop.wait(WORKER_PROGRESS_SECONDS):
            try:
                if not self.sync_progress():
                    break
            except Exception:
                # 监控写库失败不应杀死执行线程；下一次心跳继续尝试。
                time.sleep(WORKER_PROGRESS_SECONDS)

    def stop_monitor(self) -> None:
        self._stop.set()

    def run_step(self, key: str, fn, input_hash: str = ""):
        """独立步骤幂等：同输入已完成则复用结果，不重复写文件/登记/索引。"""
        from core.storage.tasks import request_fingerprint
        ih = input_hash or request_fingerprint({"step": key})
        old = self.store.step(self.job_id, key)
        if old and old.get("state") == "done" and old.get("input_hash") == ih:
            import json
            try:
                return json.loads(old.get("output_ref") or "null")
            except (TypeError, ValueError):
                return None
        if old and old.get("state") == "done" and old.get("input_hash") != ih:
            raise NeedsReview(f"步骤 {key} 的输入已变化，拒绝静默复用旧产物")
        self.store.begin_step(self.job_id, key, ih)
        try:
            out = fn()
        except Exception as exc:
            self.store.finish_step(self.job_id, key, state="failed", error=type(exc).__name__)
            raise
        import json
        self.store.finish_step(self.job_id, key, state="done",
                               output_ref=json.dumps(out, ensure_ascii=False))
        return out


def _new_legacy(job: dict) -> dict:
    kind = job["kind"]
    payload = job.get("payload") or {}
    keyword = payload.get("keyword")
    if not keyword:
        if kind == "trip" or kind == "trip_change":
            keyword = f"{payload.get('city', '')} {payload.get('days', '')}天行程"
        elif kind == "heat":
            keyword = f"{payload.get('city', '')} 热度榜"
        else:
            keyword = payload.get("question") or "任务"
    return {
        "id": job["id"], "kind": kind, "keyword": keyword,
        "mode": payload.get("mode") or ("trip" if kind in ("trip", "trip_change") else kind),
        "status": "running", "stage": "排队中", "log": [], "result": None,
        "error": None, "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "trace_id": job.get("trace_id"),
    }


def _pipeline_handler(job: dict, ctx: JobContext):
    """把持久任务接到现有业务编排；JOBS 仅供旧接口读取。"""
    from service import heatrefresh, research, trip
    from service.research import JOBS, _LOCK

    kind = job["kind"]
    payload = job.get("payload") or {}
    legacy = _new_legacy(job)
    ctx.attach_legacy(legacy)
    with _LOCK:
        JOBS[job["id"]] = legacy
    try:
        if kind == "trip":
            trip._run_trip(
                job["id"], payload["city"], int(payload["days"]), payload.get("hotel", ""),
                payload.get("spots"), payload.get("preferences", ""),
                payload.get("preference_mode", "均衡"), payload.get("start_date"),
                payload.get("excluded_spots"))
        elif kind == "research":
            preset = research.MODES.get(payload.get("mode", "standard"),
                                        research.MODES["standard"])
            research._run_job(job["id"], payload["keyword"], preset["limit"],
                              preset["comments"], preset["asr"], bool(payload.get("force")))
        elif kind == "heat":
            heatrefresh._run_heat(job["id"], payload["city"])
        elif kind == "trip_change":
            from service.report_versions import run_change_job
            run_change_job(job, ctx)
        else:
            raise NeedsReview(f"未知的 pipeline 任务类型：{kind}")
    finally:
        # 最后一次同步，确保终态 stage/日志进入持久事件。
        try:
            ctx.sync_progress(force=True)
        except Exception:
            pass
        with _LOCK:
            JOBS.pop(job["id"], None)

    status = legacy.get("status")
    if status == "done" and legacy.get("result") is not None:
        return legacy["result"]
    if status == "cancelled" or ctx.cancel_requested:
        raise JobCancelled()
    if ctx._lost:
        raise NeedsReview("执行租约已失效，结果未写入；旧进程可能仍在运行")
    raise NeedsReview(legacy.get("error") or "任务未产出结果，需要人工复核")


def _query_handler(job: dict, ctx: JobContext):
    from pipeline.ask_agent import run_agent_job
    return run_agent_job(job, ctx)


HANDLERS = {"pipeline": _pipeline_handler, "query": _query_handler}


class TaskWorker:
    def __init__(self, store=None, *, queue_name: str = "pipeline", capacity: int = 1,
                 handler=None, worker_id: str | None = None, poll_seconds: float | None = None,
                 singleton: bool = False):
        self.store = store or task_store()
        self.queue_name = queue_name
        self.capacity = max(1, int(capacity))
        self.handler = handler or HANDLERS[queue_name]
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{queue_name}:{uuid.uuid4().hex[:6]}")
        self.poll_seconds = WORKER_POLL_SECONDS if poll_seconds is None else poll_seconds
        self.singleton = singleton
        self._singleton_id = None
        self._stop = threading.Event()
        self._thread = None
        self._executor = None
        self._futures: dict = {}
        self._recover_every = 30.0
        self._last_recover = 0.0

    def start(self) -> "TaskWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._recover()
        if self.singleton:
            self._singleton_id = self.store.acquire_singleton(
                f"worker:{self.queue_name}", self.worker_id, WORKER_LEASE_SECONDS)
            if self._singleton_id is None:
                raise RuntimeError(
                    f"{self.queue_name} 执行器容量已占满：首版该队列只允许一个执行节点")
        self._executor = ThreadPoolExecutor(max_workers=self.capacity,
                                            thread_name_prefix=f"{self.queue_name}-worker")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"{self.queue_name}-loop",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=wait)
        if self._singleton_id:
            self.store.release_singleton(self._singleton_id)
            self._singleton_id = None

    def _recover(self):
        try:
            self.store.recover_expired()
        except Exception:
            pass
        self._last_recover = time.time()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.time() - self._last_recover >= self._recover_every:
                    self._recover()
                if self._singleton_id and not self.store.renew_singleton(
                        self._singleton_id, WORKER_LEASE_SECONDS):
                    self._singleton_id = self.store.acquire_singleton(
                        f"worker:{self.queue_name}", self.worker_id, WORKER_LEASE_SECONDS)
                    if self._singleton_id is None:
                        time.sleep(self.poll_seconds)
                        continue
                done = [f for f in self._futures if f.done()]
                for fut in done:
                    self._futures.pop(fut, None)
                    try:
                        fut.result()
                    except Exception:
                        pass
                while len(self._futures) < self.capacity and not self._stop.is_set():
                    job = self.store.claim(self.queue_name, self.worker_id,
                                           WORKER_LEASE_SECONDS)
                    if job is None:
                        break
                    fut = self._executor.submit(self.execute_job, job)
                    self._futures[fut] = job["id"]
                self._stop.wait(self.poll_seconds)
            except Exception:
                traceback.print_exc()
                self._stop.wait(min(5.0, self.poll_seconds * 2))

    def execute_job(self, job: dict) -> dict:
        ctx = JobContext(self.store, job, self.worker_id)
        monitor = threading.Thread(target=ctx.monitor, daemon=True,
                                   name=f"monitor-{job['id']}")
        monitor.start()
        try:
            result = self.handler(job, ctx)
            try:
                ctx.sync_progress(force=True)
            except Exception:
                pass
            quality = quality_status(result or {}) if isinstance(result, dict) else None
            ok = self.store.complete(
                job["id"], self.worker_id, job["generation"], state=SUCCEEDED,
                result=result if isinstance(result, dict) else {"value": result},
                quality_status=quality,
                result_ref=(result or {}).get("report_path") if isinstance(result, dict) else None)
            if not ok:
                self.store.append_event(job["id"], "stale_write_rejected",
                                        {"worker": self.worker_id,
                                         "generation": job["generation"]})
            elif isinstance(result, dict) and result.get("report_name"):
                try:
                    from service.task_service import ensure_version_for_job
                    ensure_version_for_job(self.store.get(job["id"]))
                except Exception as exc:
                    self.store.append_event(job["id"], "version_register_failed",
                                            {"error": type(exc).__name__})
            return {"ok": ok, "state": SUCCEEDED if ok else "stale"}
        except JobCancelled:
            self.store.complete(job["id"], self.worker_id, job["generation"],
                                state=CANCELLED, error_code="cancel_requested")
            return {"ok": True, "state": CANCELLED}
        except NeedsReview as exc:
            self.store.mark_needs_review(job["id"], self.worker_id, job["generation"],
                                         error_code="needs_review",
                                         error_detail=str(exc)[:2000])
            return {"ok": True, "state": "needs_review"}
        except RetryableError as exc:
            if int(job.get("attempt") or 0) < int(job.get("max_attempts") or WORKER_MAX_ATTEMPTS):
                self.store.schedule_retry(
                    job["id"], self.worker_id, job["generation"],
                    error_code="retryable", error_detail=str(exc)[:2000],
                    delay_seconds=WORKER_RETRY_DELAY_SECONDS)
                return {"ok": True, "state": "retry_wait"}
            self.store.complete(job["id"], self.worker_id, job["generation"],
                                state=FAILED, error_code="retry_exhausted",
                                error_detail=str(exc)[:2000])
            return {"ok": True, "state": FAILED}
        except Exception as exc:
            # 未知外部状态：不自动重放，保留现场等人工判断。
            self.store.mark_needs_review(
                job["id"], self.worker_id, job["generation"],
                error_code=type(exc).__name__, error_detail=str(exc)[:2000])
            return {"ok": True, "state": "needs_review"}
        finally:
            ctx.stop_monitor()
            monitor.join(timeout=1)

    # —— 测试/一次性运行入口 ——

    def run_once(self) -> int:
        if self.singleton and self._singleton_id is None:
            self._singleton_id = self.store.acquire_singleton(
                f"worker:{self.queue_name}", self.worker_id, WORKER_LEASE_SECONDS)
            if self._singleton_id is None:
                raise RuntimeError(f"{self.queue_name} 执行器容量已占满")
        job = self.store.claim(self.queue_name, self.worker_id, WORKER_LEASE_SECONDS)
        if job is None:
            return 0
        self.execute_job(job)
        return 1

    def drain(self, limit: int = 1000) -> int:
        done = 0
        while done < limit:
            n = self.run_once()
            if not n:
                break
            done += n
        if self._singleton_id:
            self.store.release_singleton(self._singleton_id)
            self._singleton_id = None
        return done


class WorkerGroup:
    """本地/嵌入模式的一组 Worker；外部服务模式可单独启动命令行。"""

    def __init__(self, store=None, *, role: str | None = None):
        self.store = store or task_store()
        self.role = (role or WORKER_ROLE).lower()
        self.workers: list[TaskWorker] = []

    def start(self) -> "WorkerGroup":
        if self.role in ("pipeline", "both"):
            self.workers.append(TaskWorker(
                self.store, queue_name="pipeline", capacity=PIPELINE_WORKER_CAPACITY,
                singleton=True).start())
        if self.role in ("query", "both"):
            self.workers.append(TaskWorker(
                self.store, queue_name="query", capacity=QUERY_WORKER_CAPACITY).start())
        return self

    def stop(self) -> None:
        for worker in self.workers:
            worker.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TravelGuide 持久任务 Worker")
    parser.add_argument("--role", choices=("pipeline", "query", "both"), default=WORKER_ROLE)
    parser.add_argument("--once", action="store_true", help="处理到队列暂时为空后退出")
    parser.add_argument("--poll", type=float, default=WORKER_POLL_SECONDS)
    args = parser.parse_args(argv)
    store = task_store()
    try:
        store.recover_expired()
    except Exception:
        pass
    workers: list[TaskWorker] = []
    if args.role in ("pipeline", "both"):
        workers.append(TaskWorker(store, queue_name="pipeline",
                                  capacity=PIPELINE_WORKER_CAPACITY,
                                  poll_seconds=args.poll, singleton=True))
    if args.role in ("query", "both"):
        workers.append(TaskWorker(store, queue_name="query",
                                  capacity=QUERY_WORKER_CAPACITY, poll_seconds=args.poll))
    if args.once:
        total = 0
        for worker in workers:
            total += worker.drain()
        print(f"processed {total} job(s)")
        return 0
    for worker in workers:
        worker.start()
    print(f"worker started role={args.role}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for worker in workers:
            worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())