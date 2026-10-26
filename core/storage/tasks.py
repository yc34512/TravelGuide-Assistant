"""SQLite 持久任务库：任务领取、租约、幂等、事件与步骤账本。

首版队列就是 task_jobs 表本身（设计 §6.5）：接收与排队在同一事务完成，
不存在“数据库已写入、消息未发布”的窗口。SQLite 没有 SKIP LOCKED，
因此用 BEGIN IMMEDIATE + 条件 UPDATE 实现短事务领取；MySQL 适配使用
FOR UPDATE SKIP LOCKED，二者合同一致。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from config import (MAX_ACTIVE_PER_OWNER, MAX_QUEUE_TOTAL, TASK_DB_PATH,
                    WORKER_MAX_ATTEMPTS)
from core.sqlite_utils import ClosingConnection

# 任务状态机（设计 §6.2）
QUEUED = "queued"
RUNNING = "running"
RETRY_WAIT = "retry_wait"
NEEDS_REVIEW = "needs_review"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"
SUCCEEDED = "succeeded"
FAILED = "failed"

ACTIVE_STATES = (QUEUED, RUNNING, RETRY_WAIT, NEEDS_REVIEW, CANCEL_REQUESTED)
RUNNABLE_STATES = (QUEUED, RETRY_WAIT)

# 旧 API/前端使用的终态词汇；存储内部不混用。
LEGACY_STATUS = {QUEUED: "running", RUNNING: "running", RETRY_WAIT: "running",
                 NEEDS_REVIEW: "running", CANCEL_REQUESTED: "running",
                 CANCELLED: "cancelled", SUCCEEDED: "done", FAILED: "error"}


class TaskError(RuntimeError):
    pass


class IdempotencyConflict(TaskError):
    pass


class QueueFull(TaskError):
    pass


class TaskNotFound(TaskError):
    pass


class TaskBusy(TaskError):
    pass


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def request_fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class SQLiteTaskStore:
    """一个进程可创建多个实例；所有状态在 SQLite 中，JOBS 仅为缓存视图。"""

    def __init__(self, path=None):
        self.path = Path(path or TASK_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                               factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_jobs (
                  id TEXT PRIMARY KEY,
                  request_id TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  queue_name TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  state TEXT NOT NULL,
                  stage TEXT NOT NULL DEFAULT '',
                  payload TEXT NOT NULL,
                  result_json TEXT,
                  error_code TEXT,
                  error_detail TEXT,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 2,
                  available_at REAL NOT NULL,
                  lease_owner TEXT,
                  lease_expires_at REAL,
                  generation INTEGER NOT NULL DEFAULT 0,
                  checkpoint_realm TEXT,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  trace_id TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  started_at REAL,
                  finished_at REAL,
                  quality_status TEXT,
                  result_ref TEXT,
                  parent_version_id TEXT,
                  base_report_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_task_owner_idem
                  ON task_jobs(owner_id, idempotency_key);
                CREATE INDEX IF NOT EXISTS ix_task_queue
                  ON task_jobs(queue_name, state, available_at, id);
                CREATE INDEX IF NOT EXISTS ix_task_lease
                  ON task_jobs(state, lease_expires_at);
                CREATE INDEX IF NOT EXISTS ix_task_owner_created
                  ON task_jobs(owner_id, created_at, id);
                CREATE TABLE IF NOT EXISTS task_requests (
                  request_id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  normalized_payload TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  UNIQUE(owner_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS task_steps (
                  job_id TEXT NOT NULL,
                  step_key TEXT NOT NULL,
                  input_hash TEXT NOT NULL,
                  state TEXT NOT NULL,
                  output_ref TEXT,
                  error TEXT,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(job_id, step_key)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  trace_id TEXT NOT NULL,
                  job_id TEXT,
                  run_id TEXT,
                  event_type TEXT NOT NULL,
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_task_events_job
                  ON task_events(job_id, event_id);
                CREATE TABLE IF NOT EXISTS principals (
                  id TEXT PRIMARY KEY,
                  token_hash TEXT UNIQUE,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_leases (
                  lease_id TEXT PRIMARY KEY,
                  resource TEXT NOT NULL,
                  owner TEXT NOT NULL,
                  job_id TEXT,
                  generation INTEGER NOT NULL DEFAULT 0,
                  expires_at REAL NOT NULL,
                  released_at REAL,
                  created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_resource_lease
                  ON resource_leases(resource, released_at, expires_at);
            """)

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
        if "cancel_requested" in out:
            out["cancel_requested"] = bool(out["cancel_requested"])
        if "state" in out:
            out["legacy_status"] = LEGACY_STATUS.get(out["state"], "running")
        return out

    # —— 提交与幂等 ——

    def submit(self, *, owner_id: str, idempotency_key: str, kind: str, queue_name: str,
               payload: dict, request_id: str | None = None,
               max_attempts: int = WORKER_MAX_ATTEMPTS, trace_id: str | None = None,
               parent_version_id: str | None = None,
               base_report_id: str | None = None) -> dict:
        if not owner_id:
            raise TaskError("owner_id 不能为空")
        key = str(idempotency_key or uuid.uuid4().hex)
        payload = dict(payload or {})
        req_hash = request_fingerprint({"kind": kind, "payload": payload})
        rid = request_id or uuid.uuid4().hex
        jid = uuid.uuid4().hex[:12]
        trace = trace_id or uuid.uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute(
                    "SELECT * FROM task_jobs WHERE owner_id=? AND idempotency_key=?",
                    (owner_id, key)).fetchone()
                if old is not None:
                    conn.rollback()
                    if old["request_hash"] != req_hash:
                        raise IdempotencyConflict("同一幂等键对应不同请求")
                    out = self._row(old)
                    out["reused"] = True
                    return out
                queued = conn.execute(
                    "SELECT COUNT(*) FROM task_jobs WHERE state IN (?,?)",
                    RUNNABLE_STATES).fetchone()[0]
                if queued >= MAX_QUEUE_TOTAL:
                    conn.rollback()
                    raise QueueFull("任务队列已满，请稍后重试")
                owner_active = conn.execute(
                    "SELECT COUNT(*) FROM task_jobs WHERE owner_id=? AND state IN "
                    "(?,?,?,?,?)", (owner_id, *ACTIVE_STATES)).fetchone()[0]
                if owner_active >= MAX_ACTIVE_PER_OWNER:
                    conn.rollback()
                    raise QueueFull("该主体未结束任务数已达上限，请等待现有任务完成")
                conn.execute(
                    "INSERT INTO task_requests(request_id,owner_id,idempotency_key,request_hash,"
                    "normalized_payload,created_at) VALUES(?,?,?,?,?,?)",
                    (rid, owner_id, key, req_hash, canonical(payload), now))
                conn.execute(
                    "INSERT INTO task_jobs(id,request_id,owner_id,idempotency_key,request_hash,"
                    "queue_name,kind,state,stage,payload,attempt,max_attempts,available_at,"
                    "checkpoint_realm,cancel_requested,trace_id,created_at,updated_at,"
                    "parent_version_id,base_report_id) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,0,?,?,?,?,?)",
                    (jid, rid, owner_id, key, req_hash, queue_name, kind, QUEUED, "排队中",
                     canonical(payload), max(1, int(max_attempts)), now,
                     None, trace, now, now, parent_version_id, base_report_id))
                self._event(conn, trace, jid, None, "job_submitted",
                            {"kind": kind, "queue": queue_name}, now)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        return {"id": jid, "job_id": jid, "request_id": rid, "state": QUEUED,
                "legacy_status": LEGACY_STATUS[QUEUED], "reused": False, "trace_id": trace}

    # —— 领取、心跳、推进 ——

    def claim(self, queue_name: str, worker_id: str, lease_seconds: float) -> dict | None:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM task_jobs WHERE queue_name=? AND state IN (?,?)"
                    " AND available_at<=? AND cancel_requested=0"
                    " ORDER BY available_at,id LIMIT 1",
                    (queue_name, *RUNNABLE_STATES, now)).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                gen = int(row["generation"]) + 1
                cur = conn.execute(
                    "UPDATE task_jobs SET state=?, lease_owner=?, lease_expires_at=?,"
                    " generation=?, attempt=attempt+1, started_at=COALESCE(started_at,?),"
                    " updated_at=? WHERE id=? AND state IN (?,?)",
                    (RUNNING, worker_id, now + lease_seconds, gen, now, now,
                     row["id"], *RUNNABLE_STATES))
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
                self._event(conn, row["trace_id"], row["id"], None, "job_claimed",
                            {"worker": worker_id, "generation": gen}, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_jobs WHERE id=?", (row["id"],)).fetchone()
        return self._row(row)

    def heartbeat(self, job_id: str, worker_id: str, generation: int,
                  lease_seconds: float) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE task_jobs SET lease_expires_at=?, updated_at=?"
                " WHERE id=? AND lease_owner=? AND generation=? AND state IN (?,?)",
                (now + lease_seconds, now, job_id, worker_id, int(generation),
                 RUNNING, CANCEL_REQUESTED))
            return cur.rowcount == 1

    def progress(self, job_id: str, worker_id: str, generation: int, *,
                 stage: str | None = None, log_messages: list[str] | None = None,
                 log_offset: int = 0) -> dict:
        """同步进度并返回当前取消标记；调用方必须检查返回的 cancel_requested。"""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM task_jobs WHERE id=? AND lease_owner=? AND generation=?",
                    (job_id, worker_id, int(generation))).fetchone()
                if row is None:
                    conn.rollback()
                    return {"owned": False, "cancel_requested": True}
                fields, values = ["updated_at=?"], [now]
                if stage is not None and stage != row["stage"]:
                    fields.append("stage=?")
                    values.append(str(stage))
                if row["cancel_requested"]:
                    fields.append("state=?")
                    values.append(CANCEL_REQUESTED)
                conn.execute("UPDATE task_jobs SET " + ",".join(fields) + " WHERE id=?",
                             (*values, job_id))
                if stage is not None and stage != row["stage"]:
                    self._event(conn, row["trace_id"], job_id, None, "stage_changed",
                                {"stage": str(stage)}, now)
                for i, message in enumerate(log_messages or []):
                    self._event(conn, row["trace_id"], job_id, None, "log",
                                {"index": int(log_offset) + i, "message": str(message)}, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"owned": True, "cancel_requested": bool(row["cancel_requested"]),
                "state": CANCEL_REQUESTED if row["cancel_requested"] else row["state"]}

    def complete(self, job_id: str, worker_id: str, generation: int, *,
                 state: str, result: dict | None = None, error_code: str | None = None,
                 error_detail: str | None = None, quality_status: str | None = None,
                 result_ref: str | None = None) -> bool:
        if state not in (SUCCEEDED, FAILED, CANCELLED):
            raise ValueError("complete 只接受终态")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM task_jobs WHERE id=? AND lease_owner=? AND generation=?",
                    (job_id, worker_id, int(generation))).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                final = CANCELLED if row["cancel_requested"] else state
                conn.execute(
                    "UPDATE task_jobs SET state=?, stage=?, result_json=?, error_code=?,"
                    " error_detail=?, quality_status=?, result_ref=?, finished_at=?,"
                    " lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                    (final, "完成" if final == SUCCEEDED else ("已取消" if final == CANCELLED else "失败"),
                     canonical(result) if result is not None else None, error_code,
                     error_detail, quality_status, result_ref, now, now, job_id))
                self._event(conn, row["trace_id"], job_id, None, "job_" + final,
                            {"state": final, "error_code": error_code,
                             "quality_status": quality_status}, now)
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    # —— 取消、重试、恢复 ——

    def cancel(self, job_id: str, owner_id: str | None = None) -> dict:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM task_jobs WHERE id=?", (job_id,)).fetchone()
                if row is None or (owner_id is not None and row["owner_id"] != owner_id):
                    conn.rollback()
                    raise TaskNotFound(job_id)
                if row["state"] in (SUCCEEDED, FAILED, CANCELLED):
                    conn.rollback()
                    return self._row(row)
                if row["state"] in RUNNABLE_STATES or row["state"] == NEEDS_REVIEW:
                    conn.execute(
                        "UPDATE task_jobs SET state=?, cancel_requested=1, finished_at=?,"
                        " updated_at=? WHERE id=?", (CANCELLED, now, now, job_id))
                    final = CANCELLED
                else:
                    conn.execute(
                        "UPDATE task_jobs SET cancel_requested=1, state=?, updated_at=?"
                        " WHERE id=?", (CANCEL_REQUESTED, now, job_id))
                    final = CANCEL_REQUESTED
                self._event(conn, row["trace_id"], job_id, None, "cancel_requested",
                            {"state": final}, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get(job_id)

    def retry(self, job_id: str, owner_id: str | None = None) -> dict:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM task_jobs WHERE id=?", (job_id,)).fetchone()
                if row is None or (owner_id is not None and row["owner_id"] != owner_id):
                    conn.rollback()
                    raise TaskNotFound(job_id)
                if row["state"] not in (FAILED, NEEDS_REVIEW):
                    conn.rollback()
                    raise TaskError("只有失败或待复核任务可以重试")
                if row["attempt"] >= row["max_attempts"]:
                    conn.rollback()
                    raise TaskError("重试次数已用尽，请新建任务")
                conn.execute(
                    "UPDATE task_jobs SET state=?, available_at=?, cancel_requested=0,"
                    " error_code=NULL, error_detail=NULL, lease_owner=NULL,"
                    " lease_expires_at=NULL, updated_at=? WHERE id=?",
                    (RETRY_WAIT, now, now, job_id))
                self._event(conn, row["trace_id"], job_id, None, "retry_scheduled",
                            {"attempt": row["attempt"], "max_attempts": row["max_attempts"]}, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get(job_id)

    def recover_expired(self, now: float | None = None) -> int:
        """租约过期的运行任务转为 needs_review；不自动启动第二个执行者。

        过期只证明“租约到期”，不证明旧进程或它发出的外部请求已经停止。
        """
        now = time.time() if now is None else now
        changed = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT * FROM task_jobs WHERE state IN (?,?) AND lease_expires_at IS NOT NULL"
                    " AND lease_expires_at < ?", (RUNNING, CANCEL_REQUESTED, now)).fetchall()
                for row in rows:
                    final = CANCELLED if row["cancel_requested"] else NEEDS_REVIEW
                    conn.execute(
                        "UPDATE task_jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL,"
                        " error_code=?, error_detail=?, updated_at=? WHERE id=?",
                        (final, None if final == CANCELLED else "lease_expired",
                         None if final == CANCELLED else "执行租约过期：无法确认旧进程或外部请求已停止",
                         now, row["id"]))
                    self._event(conn, row["trace_id"], row["id"], None,
                                "lease_expired", {"final_state": final}, now)
                    changed += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return changed

    def schedule_retry(self, job_id: str, worker_id: str, generation: int, *,
                       error_code: str, error_detail: str, delay_seconds: float) -> bool:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM task_jobs WHERE id=? AND lease_owner=? AND generation=?",
                    (job_id, worker_id, int(generation))).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                if row["cancel_requested"]:
                    conn.execute(
                        "UPDATE task_jobs SET state=?, stage='已取消', finished_at=?,"
                        " lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                        (CANCELLED, now, now, job_id))
                    event = "job_cancelled"
                    final = CANCELLED
                else:
                    conn.execute(
                        "UPDATE task_jobs SET state=?, available_at=?, error_code=?,"
                        " error_detail=?, lease_owner=NULL, lease_expires_at=NULL,"
                        " updated_at=? WHERE id=?",
                        (RETRY_WAIT, now + max(0.0, delay_seconds), error_code,
                         error_detail, now, job_id))
                    event = "retry_scheduled"
                    final = RETRY_WAIT
                self._event(conn, row["trace_id"], job_id, None, event,
                            {"state": final, "error_code": error_code,
                             "delay_seconds": delay_seconds}, now)
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def mark_needs_review(self, job_id: str, worker_id: str, generation: int, *,
                          error_code: str, error_detail: str) -> bool:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM task_jobs WHERE id=? AND lease_owner=? AND generation=?",
                    (job_id, worker_id, int(generation))).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                final = CANCELLED if row["cancel_requested"] else NEEDS_REVIEW
                conn.execute(
                    "UPDATE task_jobs SET state=?, stage=?, error_code=?, error_detail=?,"
                    " lease_owner=NULL, lease_expires_at=NULL, finished_at=?, updated_at=?"
                    " WHERE id=?",
                    (final, "已取消" if final == CANCELLED else "待人工复核",
                     None if final == CANCELLED else error_code,
                     None if final == CANCELLED else error_detail, now, now, job_id))
                self._event(conn, row["trace_id"], job_id, None,
                            "job_" + final, {"error_code": error_code}, now)
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    # —— 执行器单例租约（首版 pipeline 容量固定为 1）——

    def acquire_slot(self, resource: str, owner: str, job_id: str, capacity: int,
                     ttl: float) -> str | None:
        """持久并发槽位（跨进程）。释放按唯一 lease_id 去重，重复回调不会多释放。"""
        now = time.time()
        lease_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE resource_leases SET released_at=? WHERE released_at IS NULL"
                    " AND expires_at<?", (now, now))
                live = conn.execute(
                    "SELECT COUNT(*) FROM resource_leases WHERE resource=?"
                    " AND released_at IS NULL AND expires_at>=?",
                    (resource, now)).fetchone()[0]
                if live >= max(1, int(capacity)):
                    conn.rollback()
                    return None
                conn.execute(
                    "INSERT INTO resource_leases(lease_id,resource,owner,job_id,generation,"
                    "expires_at,released_at,created_at) VALUES(?,?,?,?,0,?,NULL,?)",
                    (lease_id, resource, owner, job_id, now + ttl, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return lease_id

    def release_slot(self, lease_id: str) -> bool:
        if not lease_id:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE resource_leases SET released_at=? WHERE lease_id=?"
                " AND released_at IS NULL", (time.time(), lease_id))
            return cur.rowcount == 1

    def acquire_singleton(self, resource: str, owner: str, ttl: float) -> str | None:
        now = time.time()
        lease_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE resource_leases SET released_at=? WHERE resource=?"
                    " AND released_at IS NULL AND expires_at<?", (now, resource, now))
                live = conn.execute(
                    "SELECT COUNT(*) FROM resource_leases WHERE resource=? AND released_at IS NULL"
                    " AND expires_at>=?", (resource, now)).fetchone()[0]
                if live:
                    conn.rollback()
                    return None
                conn.execute(
                    "INSERT INTO resource_leases(lease_id,resource,owner,job_id,generation,"
                    "expires_at,released_at,created_at) VALUES(?,?,?,?,0,?,NULL,?)",
                    (lease_id, resource, owner, None, now + ttl, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return lease_id

    def renew_singleton(self, lease_id: str, ttl: float) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE resource_leases SET expires_at=? WHERE lease_id=?"
                " AND released_at IS NULL", (now + ttl, lease_id))
            return cur.rowcount == 1

    def release_singleton(self, lease_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE resource_leases SET released_at=? WHERE lease_id=?",
                         (time.time(), lease_id))

    def attach_result_meta(self, job_id: str, meta: dict) -> None:
        """在不改变终态的前提下补充报告版本引用（幂等）。"""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT result_json FROM task_jobs WHERE id=?",
                               (job_id,)).fetchone()
            if row is None:
                raise TaskNotFound(job_id)
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                result = {}
            result.update(meta or {})
            conn.execute("UPDATE task_jobs SET result_json=?, updated_at=? WHERE id=?",
                         (canonical(result), now, job_id))

    # —— 查询与事件 ——

    def get(self, job_id: str, owner_id: str | None = None) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_jobs WHERE id=?", (job_id,)).fetchone()
        out = self._row(row)
        if out is None or (owner_id is not None and out["owner_id"] != owner_id):
            raise TaskNotFound(job_id)
        return out

    def find_by_idempotency(self, owner_id: str, key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_jobs WHERE owner_id=? AND idempotency_key=?",
                (owner_id, key)).fetchone()
        return self._row(row)

    def list_jobs(self, owner_id: str | None = None, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            if owner_id is None:
                rows = conn.execute("SELECT * FROM task_jobs ORDER BY created_at DESC LIMIT ?",
                                    (max(1, int(limit)),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                    (owner_id, max(1, int(limit)))).fetchall()
        return [self._row(r) for r in rows]

    def events(self, job_id: str, after: int = 0, owner_id: str | None = None) -> list[dict]:
        job = self.get(job_id, owner_id)   # 越权与不存在统一 404
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,trace_id,job_id,run_id,event_type,payload,created_at"
                " FROM task_events WHERE job_id=? AND event_id>? ORDER BY event_id LIMIT 1000",
                (job["id"], max(0, int(after)))).fetchall()
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
        job = self.get(job_id)
        with self._connect() as conn:
            cur = self._event(conn, job["trace_id"], job_id, run_id, event_type,
                              dict(payload or {}), time.time())
            conn.commit()
            return int(cur.lastrowid)

    def _event(self, conn, trace_id, job_id, run_id, event_type, payload, now):
        return conn.execute(
            "INSERT INTO task_events(trace_id,job_id,run_id,event_type,payload,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (trace_id, job_id, run_id, event_type, canonical(payload or {}), now))

    # —— 步骤账本（文件/登记/索引等独立步骤的幂等基础）——

    def step(self, job_id: str, step_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_steps WHERE job_id=? AND step_key=?",
                (job_id, step_key)).fetchone()
        return dict(row) if row else None

    def begin_step(self, job_id: str, step_key: str, input_hash: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_steps(job_id,step_key,input_hash,state,attempts,updated_at)"
                " VALUES(?,?,?,'running',1,?) ON CONFLICT(job_id,step_key) DO UPDATE SET"
                " input_hash=excluded.input_hash,state='running',error=NULL,"
                " attempts=task_steps.attempts+1,updated_at=excluded.updated_at",
                (job_id, step_key, input_hash, now))

    def finish_step(self, job_id: str, step_key: str, *, state: str,
                    output_ref: str | None = None, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE task_steps SET state=?,output_ref=?,error=?,updated_at=?"
                " WHERE job_id=? AND step_key=?",
                (state, output_ref, error, time.time(), job_id, step_key))

    # —— 主体与容量 ——

    def put_principal(self, owner_id: str, token_hash: str | None = None,
                      enabled: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO principals(id,token_hash,enabled,created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,"
                " enabled=excluded.enabled",
                (owner_id, token_hash, 1 if enabled else 0, time.time()))

    def principal_by_token_hash(self, token_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM principals WHERE token_hash=? AND enabled=1",
                (token_hash,)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT queue_name,state,COUNT(*) AS n FROM task_jobs GROUP BY queue_name,state"
            ).fetchall()
        out: dict = {"by_queue": {}, "total": 0}
        for row in rows:
            out["by_queue"].setdefault(row["queue_name"], {})[row["state"]] = row["n"]
            out["total"] += row["n"]
        return out