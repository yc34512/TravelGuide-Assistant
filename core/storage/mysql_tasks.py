"""MySQL 任务库适配（InnoDB，短事务 + FOR UPDATE SKIP LOCKED）。

仅在 STORAGE_BACKEND=mysql 且配置 MYSQL_DSN 时使用；缺少驱动或连接失败会
明确报错，不偷偷回退 SQLite。真实 MySQL 集成需要可用服务器（本机当前未启动）。
"""
from __future__ import annotations

import json
import time
import uuid

from config import MAX_ACTIVE_PER_OWNER, MAX_QUEUE_TOTAL, MYSQL_DSN, WORKER_MAX_ATTEMPTS
from core.storage.tasks import (ACTIVE_STATES, CANCELLED, CANCEL_REQUESTED, FAILED,
                                LEGACY_STATUS, NEEDS_REVIEW, QUEUED, RETRY_WAIT,
                                RUNNABLE_STATES, RUNNING, SUCCEEDED, IdempotencyConflict,
                                QueueFull, TaskError, TaskNotFound, canonical,
                                request_fingerprint)


def _engine(dsn: str | None = None):
    try:
        from sqlalchemy import create_engine
    except Exception as exc:  # pragma: no cover - 依赖缺失路径
        raise RuntimeError("MySQL 模式需要 SQLAlchemy 与 PyMySQL：pip install -r requirements-service.txt") from exc
    value = (dsn or MYSQL_DSN or "").strip()
    if not value:
        raise RuntimeError("STORAGE_BACKEND=mysql 但未配置 MYSQL_DSN")
    if value.startswith("mysql://"):
        value = "mysql+pymysql://" + value[len("mysql://"):]
    elif value.startswith("mysql+"):
        pass
    else:
        value = "mysql+pymysql://" + value
    return create_engine(value, pool_pre_ping=True, pool_size=5, max_overflow=5,
                         connect_args={"charset": "utf8mb4"})


DDL = [
    """CREATE TABLE IF NOT EXISTS task_jobs (
      id VARCHAR(32) PRIMARY KEY,
      request_id VARCHAR(64) NOT NULL,
      owner_id VARCHAR(128) NOT NULL,
      idempotency_key VARCHAR(128) NOT NULL,
      request_hash CHAR(64) NOT NULL,
      queue_name VARCHAR(32) NOT NULL,
      kind VARCHAR(32) NOT NULL,
      state VARCHAR(32) NOT NULL,
      stage VARCHAR(255) NOT NULL DEFAULT '',
      payload LONGTEXT NOT NULL,
      result_json LONGTEXT NULL,
      error_code VARCHAR(64) NULL,
      error_detail TEXT NULL,
      attempt INT NOT NULL DEFAULT 0,
      max_attempts INT NOT NULL DEFAULT 2,
      available_at DOUBLE NOT NULL,
      lease_owner VARCHAR(255) NULL,
      lease_expires_at DOUBLE NULL,
      generation INT NOT NULL DEFAULT 0,
      checkpoint_realm VARCHAR(128) NULL,
      cancel_requested TINYINT NOT NULL DEFAULT 0,
      trace_id VARCHAR(64) NOT NULL,
      created_at DOUBLE NOT NULL,
      updated_at DOUBLE NOT NULL,
      started_at DOUBLE NULL,
      finished_at DOUBLE NULL,
      quality_status VARCHAR(32) NULL,
      result_ref TEXT NULL,
      parent_version_id VARCHAR(64) NULL,
      base_report_id VARCHAR(64) NULL,
      UNIQUE KEY ux_task_owner_idem (owner_id, idempotency_key),
      KEY ix_task_queue (queue_name, state, available_at, id),
      KEY ix_task_lease (state, lease_expires_at),
      KEY ix_task_owner_created (owner_id, created_at, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS task_steps (
      job_id VARCHAR(32) NOT NULL,
      step_key VARCHAR(128) NOT NULL,
      input_hash CHAR(64) NOT NULL,
      state VARCHAR(32) NOT NULL,
      output_ref LONGTEXT NULL,
      error TEXT NULL,
      attempts INT NOT NULL DEFAULT 0,
      updated_at DOUBLE NOT NULL,
      PRIMARY KEY(job_id, step_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS task_events (
      event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
      trace_id VARCHAR(64) NOT NULL,
      job_id VARCHAR(32) NULL,
      run_id VARCHAR(64) NULL,
      event_type VARCHAR(64) NOT NULL,
      payload LONGTEXT NOT NULL,
      created_at DOUBLE NOT NULL,
      KEY ix_task_events_job (job_id, event_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admission_gate (
      scope VARCHAR(32) PRIMARY KEY,
      version BIGINT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB""",
    """INSERT IGNORE INTO admission_gate(scope, version) VALUES ('global', 0)""",
    """CREATE TABLE IF NOT EXISTS principals (
      id VARCHAR(128) PRIMARY KEY,
      token_hash CHAR(64) UNIQUE NULL,
      enabled TINYINT NOT NULL DEFAULT 1,
      created_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS resource_leases (
      lease_id VARCHAR(64) PRIMARY KEY,
      resource VARCHAR(128) NOT NULL,
      owner VARCHAR(255) NOT NULL,
      job_id VARCHAR(32) NULL,
      generation INT NOT NULL DEFAULT 0,
      expires_at DOUBLE NOT NULL,
      released_at DOUBLE NULL,
      created_at DOUBLE NOT NULL,
      KEY ix_resource_lease (resource, released_at, expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


class MySQLTaskStore:
    def __init__(self, dsn: str | None = None):
        self.engine = _engine(dsn)
        with self.engine.begin() as conn:
            for statement in DDL:
                conn.exec_driver_sql(statement)

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("payload", "result_json"):
            if out.get(key):
                try:
                    out[key] = json.loads(out[key])
                except (TypeError, ValueError):
                    pass
        out["cancel_requested"] = bool(out.get("cancel_requested"))
        out["legacy_status"] = LEGACY_STATUS.get(out.get("state"), "running")
        return out

    def submit(self, *, owner_id: str, idempotency_key: str, kind: str, queue_name: str,
               payload: dict, request_id: str | None = None,
               max_attempts: int = WORKER_MAX_ATTEMPTS, trace_id: str | None = None,
               parent_version_id: str | None = None,
               base_report_id: str | None = None) -> dict:
        from sqlalchemy import text
        key = str(idempotency_key or uuid.uuid4().hex)
        req_hash = request_fingerprint({"kind": kind, "payload": payload})
        rid, jid, trace = (request_id or uuid.uuid4().hex, uuid.uuid4().hex[:12],
                           trace_id or uuid.uuid4().hex)
        now = time.time()
        with self.engine.begin() as conn:
            old = conn.execute(text(
                "SELECT * FROM task_jobs WHERE owner_id=:o AND idempotency_key=:k FOR UPDATE"),
                {"o": owner_id, "k": key}).mappings().first()
            if old is not None:
                if old["request_hash"] != req_hash:
                    raise IdempotencyConflict("同一幂等键对应不同请求")
                out = self._row(old)
                out["reused"] = True
                return out
            # 串行化容量检查：单行 gate 锁，避免事务外 COUNT 后插入的竞态。
            conn.execute(text("SELECT version FROM admission_gate WHERE scope='global' FOR UPDATE"))
            queued = conn.execute(text(
                "SELECT COUNT(*) FROM task_jobs WHERE state IN ('queued','retry_wait')")).scalar_one()
            if queued >= MAX_QUEUE_TOTAL:
                raise QueueFull("任务队列已满，请稍后重试")
            owner_active = conn.execute(text(
                "SELECT COUNT(*) FROM task_jobs WHERE owner_id=:o AND state IN"
                " ('queued','running','retry_wait','needs_review','cancel_requested')"),
                {"o": owner_id}).scalar_one()
            if owner_active >= MAX_ACTIVE_PER_OWNER:
                raise QueueFull("该主体未结束任务数已达上限，请等待现有任务完成")
            conn.execute(text(
                "INSERT INTO task_jobs(id,request_id,owner_id,idempotency_key,request_hash,"
                "queue_name,kind,state,stage,payload,attempt,max_attempts,available_at,"
                "cancel_requested,trace_id,created_at,updated_at,parent_version_id,base_report_id)"
                " VALUES(:id,:rid,:owner,:key,:hash,:queue,:kind,'queued','排队中',:payload,"
                "0,:max_attempts,:now,0,:trace,:now,:now,:parent,:base)"),
                {"id": jid, "rid": rid, "owner": owner_id, "key": key, "hash": req_hash,
                 "queue": queue_name, "kind": kind, "payload": canonical(payload),
                 "max_attempts": max(1, int(max_attempts)), "now": now, "trace": trace,
                 "parent": parent_version_id, "base": base_report_id})
            self._event(conn, trace, jid, None, "job_submitted",
                        {"kind": kind, "queue": queue_name}, now)
        return {"id": jid, "job_id": jid, "request_id": rid, "state": QUEUED,
                "legacy_status": LEGACY_STATUS[QUEUED], "reused": False, "trace_id": trace}

    def claim(self, queue_name: str, worker_id: str, lease_seconds: float) -> dict | None:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT * FROM task_jobs WHERE queue_name=:q AND state IN ('queued','retry_wait')"
                " AND available_at<=:now AND cancel_requested=0"
                " ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"),
                {"q": queue_name, "now": now}).mappings().first()
            if row is None:
                return None
            gen = int(row["generation"]) + 1
            conn.execute(text(
                "UPDATE task_jobs SET state='running',lease_owner=:w,lease_expires_at=:exp,"
                "generation=:gen,attempt=attempt+1,started_at=COALESCE(started_at,:now),"
                "updated_at=:now WHERE id=:id"),
                {"w": worker_id, "exp": now + lease_seconds, "gen": gen, "now": now,
                 "id": row["id"]})
            self._event(conn, row["trace_id"], row["id"], None, "job_claimed",
                        {"worker": worker_id, "generation": gen}, now)
            out = self._row(row)
            out.update(state=RUNNING, lease_owner=worker_id, lease_expires_at=now + lease_seconds,
                       generation=gen, attempt=int(row["attempt"]) + 1,
                       started_at=row["started_at"] or now, legacy_status="running")
            return out

    def heartbeat(self, job_id: str, worker_id: str, generation: int,
                  lease_seconds: float) -> bool:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "UPDATE task_jobs SET lease_expires_at=:exp,updated_at=:now"
                " WHERE id=:id AND lease_owner=:w AND generation=:gen"
                " AND state IN ('running','cancel_requested')"),
                {"exp": now + lease_seconds, "now": now, "id": job_id, "w": worker_id,
                 "gen": int(generation)})
            return cur.rowcount == 1

    def progress(self, job_id: str, worker_id: str, generation: int, *,
                 stage: str | None = None, log_messages: list[str] | None = None,
                 log_offset: int = 0) -> dict:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT * FROM task_jobs WHERE id=:id AND lease_owner=:w AND generation=:gen"
                " FOR UPDATE"), {"id": job_id, "w": worker_id, "gen": int(generation)}).mappings().first()
            if row is None:
                return {"owned": False, "cancel_requested": True}
            cancel = bool(row["cancel_requested"])
            sets = ["updated_at=:now"]
            params = {"now": now, "id": job_id}
            if stage is not None and stage != row["stage"]:
                sets.append("stage=:stage")
                params["stage"] = str(stage)
            if cancel:
                sets.append("state='cancel_requested'")
            conn.execute(text("UPDATE task_jobs SET " + ",".join(sets) + " WHERE id=:id"), params)
            if stage is not None and stage != row["stage"]:
                self._event(conn, row["trace_id"], job_id, None, "stage_changed",
                            {"stage": str(stage)}, now)
            for i, message in enumerate(log_messages or []):
                self._event(conn, row["trace_id"], job_id, None, "log",
                            {"index": int(log_offset) + i, "message": str(message)}, now)
            return {"owned": True, "cancel_requested": cancel,
                    "state": CANCEL_REQUESTED if cancel else row["state"]}

    def complete(self, job_id: str, worker_id: str, generation: int, *,
                 state: str, result: dict | None = None, error_code: str | None = None,
                 error_detail: str | None = None, quality_status: str | None = None,
                 result_ref: str | None = None) -> bool:
        from sqlalchemy import text
        if state not in (SUCCEEDED, FAILED, CANCELLED):
            raise ValueError("complete 只接受终态")
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT * FROM task_jobs WHERE id=:id AND lease_owner=:w AND generation=:gen"
                " FOR UPDATE"), {"id": job_id, "w": worker_id, "gen": int(generation)}).mappings().first()
            if row is None:
                return False
            final = CANCELLED if row["cancel_requested"] else state
            conn.execute(text(
                "UPDATE task_jobs SET state=:state,stage=:stage,result_json=:result,"
                "error_code=:error_code,error_detail=:error_detail,quality_status=:quality,"
                "result_ref=:result_ref,finished_at=:now,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=:now WHERE id=:id"),
                {"state": final, "stage": "完成" if final == SUCCEEDED else
                 ("已取消" if final == CANCELLED else "失败"),
                 "result": canonical(result) if result is not None else None,
                 "error_code": error_code, "error_detail": error_detail,
                 "quality": quality_status, "result_ref": result_ref, "now": now, "id": job_id})
            self._event(conn, row["trace_id"], job_id, None, "job_" + final,
                        {"state": final, "error_code": error_code,
                         "quality_status": quality_status}, now)
            return True

    def schedule_retry(self, job_id: str, worker_id: str, generation: int, *,
                       error_code: str, error_detail: str, delay_seconds: float) -> bool:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT * FROM task_jobs WHERE id=:id AND lease_owner=:w AND generation=:gen"
                " FOR UPDATE"), {"id": job_id, "w": worker_id, "gen": int(generation)}).mappings().first()
            if row is None:
                return False
            final = CANCELLED if row["cancel_requested"] else RETRY_WAIT
            conn.execute(text(
                "UPDATE task_jobs SET state=:state,available_at=:avail,error_code=:code,"
                "error_detail=:detail,lease_owner=NULL,lease_expires_at=NULL,updated_at=:now"
                " WHERE id=:id"),
                {"state": final, "avail": now + max(0.0, delay_seconds), "code": error_code,
                 "detail": error_detail, "now": now, "id": job_id})
            self._event(conn, row["trace_id"], job_id, None,
                        "job_cancelled" if final == CANCELLED else "retry_scheduled",
                        {"state": final, "error_code": error_code}, now)
            return True

    def mark_needs_review(self, job_id: str, worker_id: str, generation: int, *,
                          error_code: str, error_detail: str) -> bool:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT * FROM task_jobs WHERE id=:id AND lease_owner=:w AND generation=:gen"
                " FOR UPDATE"), {"id": job_id, "w": worker_id, "gen": int(generation)}).mappings().first()
            if row is None:
                return False
            final = CANCELLED if row["cancel_requested"] else NEEDS_REVIEW
            conn.execute(text(
                "UPDATE task_jobs SET state=:state,stage=:stage,error_code=:code,"
                "error_detail=:detail,lease_owner=NULL,lease_expires_at=NULL,finished_at=:now,"
                "updated_at=:now WHERE id=:id"),
                {"state": final, "stage": "已取消" if final == CANCELLED else "待人工复核",
                 "code": None if final == CANCELLED else error_code,
                 "detail": None if final == CANCELLED else error_detail,
                 "now": now, "id": job_id})
            self._event(conn, row["trace_id"], job_id, None, "job_" + final,
                        {"error_code": error_code}, now)
            return True

    def cancel(self, job_id: str, owner_id: str | None = None) -> dict:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM task_jobs WHERE id=:id FOR UPDATE"),
                               {"id": job_id}).mappings().first()
            if row is None or (owner_id is not None and row["owner_id"] != owner_id):
                raise TaskNotFound(job_id)
            if row["state"] in (SUCCEEDED, FAILED, CANCELLED):
                return self._row(row)
            if row["state"] in RUNNABLE_STATES or row["state"] == NEEDS_REVIEW:
                conn.execute(text(
                    "UPDATE task_jobs SET state='cancelled',cancel_requested=1,"
                    "finished_at=:now,updated_at=:now WHERE id=:id"), {"now": now, "id": job_id})
                final = CANCELLED
            else:
                conn.execute(text(
                    "UPDATE task_jobs SET cancel_requested=1,state='cancel_requested',"
                    "updated_at=:now WHERE id=:id"), {"now": now, "id": job_id})
                final = CANCEL_REQUESTED
            self._event(conn, row["trace_id"], job_id, None, "cancel_requested",
                        {"state": final}, now)
        return self.get(job_id)

    def retry(self, job_id: str, owner_id: str | None = None) -> dict:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM task_jobs WHERE id=:id FOR UPDATE"),
                               {"id": job_id}).mappings().first()
            if row is None or (owner_id is not None and row["owner_id"] != owner_id):
                raise TaskNotFound(job_id)
            if row["state"] not in (FAILED, NEEDS_REVIEW):
                raise TaskError("只有失败或待复核任务可以重试")
            if row["attempt"] >= row["max_attempts"]:
                raise TaskError("重试次数已用尽，请新建任务")
            conn.execute(text(
                "UPDATE task_jobs SET state='retry_wait',available_at=:now,cancel_requested=0,"
                "error_code=NULL,error_detail=NULL,lease_owner=NULL,lease_expires_at=NULL,"
                "updated_at=:now WHERE id=:id"), {"now": now, "id": job_id})
            self._event(conn, row["trace_id"], job_id, None, "retry_scheduled",
                        {"attempt": row["attempt"]}, now)
        return self.get(job_id)

    def recover_expired(self, now: float | None = None) -> int:
        from sqlalchemy import text
        now = time.time() if now is None else now
        changed = 0
        with self.engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT * FROM task_jobs WHERE state IN ('running','cancel_requested')"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at<:now"
                " FOR UPDATE SKIP LOCKED"), {"now": now}).mappings().all()
            for row in rows:
                final = CANCELLED if row["cancel_requested"] else NEEDS_REVIEW
                conn.execute(text(
                    "UPDATE task_jobs SET state=:state,lease_owner=NULL,lease_expires_at=NULL,"
                    "error_code=:code,error_detail=:detail,updated_at=:now WHERE id=:id"),
                    {"state": final, "code": None if final == CANCELLED else "lease_expired",
                     "detail": None if final == CANCELLED else
                     "执行租约过期：无法确认旧进程或外部请求已停止",
                     "now": now, "id": row["id"]})
                self._event(conn, row["trace_id"], row["id"], None, "lease_expired",
                            {"final_state": final}, now)
                changed += 1
        return changed

    def get(self, job_id: str, owner_id: str | None = None) -> dict:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM task_jobs WHERE id=:id"),
                               {"id": job_id}).mappings().first()
        out = self._row(row)
        if out is None or (owner_id is not None and out["owner_id"] != owner_id):
            raise TaskNotFound(job_id)
        return out

    def list_jobs(self, owner_id: str | None = None, limit: int = 50) -> list[dict]:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            if owner_id is None:
                rows = conn.execute(text(
                    "SELECT * FROM task_jobs ORDER BY created_at DESC LIMIT :n"),
                    {"n": max(1, int(limit))}).mappings().all()
            else:
                rows = conn.execute(text(
                    "SELECT * FROM task_jobs WHERE owner_id=:o ORDER BY created_at DESC LIMIT :n"),
                    {"o": owner_id, "n": max(1, int(limit))}).mappings().all()
        return [self._row(r) for r in rows]

    def events(self, job_id: str, after: int = 0, owner_id: str | None = None) -> list[dict]:
        from sqlalchemy import text
        self.get(job_id, owner_id)
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT event_id,trace_id,job_id,run_id,event_type,payload,created_at"
                " FROM task_events WHERE job_id=:id AND event_id>:after"
                " ORDER BY event_id LIMIT 1000"),
                {"id": job_id, "after": max(0, int(after))}).mappings().all()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            out.append(item)
        return out

    def append_event(self, job_id: str, event_type: str, payload: dict | None = None,
                     run_id: str | None = None) -> int:
        from sqlalchemy import text
        job = self.get(job_id)
        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "INSERT INTO task_events(trace_id,job_id,run_id,event_type,payload,created_at)"
                " VALUES(:trace,:id,:run,:type,:payload,:now)"),
                {"trace": job["trace_id"], "id": job_id, "run": run_id,
                 "type": event_type, "payload": canonical(payload or {}),
                 "now": time.time()})
            return int(cur.lastrowid or 0)

    def _event(self, conn, trace_id, job_id, run_id, event_type, payload, now):
        from sqlalchemy import text
        conn.execute(text(
            "INSERT INTO task_events(trace_id,job_id,run_id,event_type,payload,created_at)"
            " VALUES(:trace,:id,:run,:type,:payload,:now)"),
            {"trace": trace_id, "id": job_id, "run": run_id, "type": event_type,
             "payload": canonical(payload or {}), "now": now})

    def step(self, job_id: str, step_key: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT * FROM task_steps WHERE job_id=:id AND step_key=:key"),
                {"id": job_id, "key": step_key}).mappings().first()
        return dict(row) if row else None

    def begin_step(self, job_id: str, step_key: str, input_hash: str) -> None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO task_steps(job_id,step_key,input_hash,state,attempts,updated_at)"
                " VALUES(:id,:key,:hash,'running',1,:now) ON DUPLICATE KEY UPDATE"
                " input_hash=VALUES(input_hash),state='running',error=NULL,"
                " attempts=attempts+1,updated_at=VALUES(updated_at)"),
                {"id": job_id, "key": step_key, "hash": input_hash, "now": time.time()})

    def finish_step(self, job_id: str, step_key: str, *, state: str,
                    output_ref: str | None = None, error: str | None = None) -> None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE task_steps SET state=:state,output_ref=:out,error=:err,updated_at=:now"
                " WHERE job_id=:id AND step_key=:key"),
                {"state": state, "out": output_ref, "err": error, "now": time.time(),
                 "id": job_id, "key": step_key})

    def attach_result_meta(self, job_id: str, meta: dict) -> None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT result_json FROM task_jobs WHERE id=:id FOR UPDATE"),
                               {"id": job_id}).mappings().first()
            if row is None:
                raise TaskNotFound(job_id)
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                result = {}
            result.update(meta or {})
            conn.execute(text("UPDATE task_jobs SET result_json=:r,updated_at=:now WHERE id=:id"),
                         {"r": canonical(result), "now": time.time(), "id": job_id})

    def put_principal(self, owner_id: str, token_hash: str | None = None,
                      enabled: bool = True) -> None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO principals(id,token_hash,enabled,created_at)"
                " VALUES(:id,:hash,:enabled,:now) ON DUPLICATE KEY UPDATE"
                " token_hash=VALUES(token_hash),enabled=VALUES(enabled)"),
                {"id": owner_id, "hash": token_hash, "enabled": 1 if enabled else 0,
                 "now": time.time()})

    def principal_by_token_hash(self, token_hash: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT * FROM principals WHERE token_hash=:hash AND enabled=1"),
                {"hash": token_hash}).mappings().first()
        return dict(row) if row else None

    def acquire_slot(self, resource: str, owner: str, job_id: str, capacity: int,
                     ttl: float) -> str | None:
        from sqlalchemy import text
        now = time.time()
        lease_id = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE resource_leases SET released_at=:now WHERE released_at IS NULL"
                " AND expires_at<:now"), {"now": now})
            live = conn.execute(text(
                "SELECT lease_id FROM resource_leases WHERE resource=:r"
                " AND released_at IS NULL AND expires_at>=:now FOR UPDATE"),
                {"r": resource, "now": now}).all()
            if len(live) >= max(1, int(capacity)):
                return None
            conn.execute(text(
                "INSERT INTO resource_leases(lease_id,resource,owner,job_id,generation,"
                "expires_at,released_at,created_at) VALUES(:id,:r,:o,:job,0,:exp,NULL,:now)"),
                {"id": lease_id, "r": resource, "o": owner, "job": job_id,
                 "exp": now + ttl, "now": now})
        return lease_id

    def release_slot(self, lease_id: str) -> bool:
        from sqlalchemy import text
        if not lease_id:
            return False
        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "UPDATE resource_leases SET released_at=:now WHERE lease_id=:id"
                " AND released_at IS NULL"), {"now": time.time(), "id": lease_id})
            return cur.rowcount == 1

    def acquire_singleton(self, resource: str, owner: str, ttl: float) -> str | None:
        from sqlalchemy import text
        now = time.time()
        lease_id = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE resource_leases SET released_at=:now WHERE resource=:r"
                " AND released_at IS NULL AND expires_at<:now"), {"now": now, "r": resource})
            live = conn.execute(text(
                "SELECT lease_id FROM resource_leases WHERE resource=:r"
                " AND released_at IS NULL AND expires_at>=:now FOR UPDATE"),
                {"r": resource, "now": now}).all()
            if live:
                return None
            conn.execute(text(
                "INSERT INTO resource_leases(lease_id,resource,owner,job_id,generation,"
                "expires_at,released_at,created_at) VALUES(:id,:r,:o,NULL,0,:exp,NULL,:now)"),
                {"id": lease_id, "r": resource, "o": owner, "exp": now + ttl, "now": now})
        return lease_id

    def renew_singleton(self, lease_id: str, ttl: float) -> bool:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "UPDATE resource_leases SET expires_at=:exp WHERE lease_id=:id"
                " AND released_at IS NULL"), {"exp": time.time() + ttl, "id": lease_id})
            return cur.rowcount == 1

    def release_singleton(self, lease_id: str) -> None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE resource_leases SET released_at=:now WHERE lease_id=:id"),
                         {"now": time.time(), "id": lease_id})

    def stats(self) -> dict:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT queue_name,state,COUNT(*) AS n FROM task_jobs GROUP BY queue_name,state"
            )).mappings().all()
        out = {"by_queue": {}, "total": 0}
        for row in rows:
            out["by_queue"].setdefault(row["queue_name"], {})[row["state"]] = int(row["n"])
            out["total"] += int(row["n"])
        return out