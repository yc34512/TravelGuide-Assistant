"""规划运行的不可变输入、请求账本与进程间互斥。只使用本地SQLite。

图检查点另存一个库；此处业务run_id用于关联API、预算与独立后处理。
外部请求不具备exactly-once：已预留而未结算的尝试在恢复时标unknown。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from importlib.metadata import version
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
GRAPH_VERSION = "planning-v1"
POLICIES = {"legacy-v1", "hard-first-v1"}


class RunError(RuntimeError):
    pass


class RunBusy(RunError):
    pass


class RunCancelled(RunError):
    pass


class BudgetExhausted(RunError):
    pass


class InvalidPlanError(RunError):
    pass


def canonical(value) -> str:
    # 档案字典的顺序影响规划提示词；持久化及指纹都保留原输入顺序。
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"),
                      allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def implementation_hash():
    """包含影响规划/报告的代码和模板；变更后不能静默续跑旧实现。"""
    root = Path(__file__).resolve().parent.parent
    files = ("core/planning_runs.py", "core/model_calls.py", "core/evidence.py",
             "pipeline/planning_graph.py", "pipeline/planning_policy.py", "pipeline/planner.py",
             "pipeline/qc.py", "pipeline/decision.py", "pipeline/trip_render.py",
             "service/planning_flow.py")
    records = [(name, hashlib.sha256((root / name).read_bytes()).hexdigest()) for name in files]
    records += [(str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
                for path in sorted((root / "templates").glob("*")) if path.is_file()]
    records += [(name, version(name)) for name in ("langgraph", "langgraph-checkpoint-sqlite")]
    return fingerprint(records)


def default_store():
    from config import DATA_DIR
    return RunStore(DATA_DIR / "planning" / "runs.db")


class RunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / "locks"
        self.lock_dir.mkdir(exist_ok=True)
        self.checkpoint_path = self.path.with_name("checkpoints.db")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY, job_id TEXT UNIQUE, input_hash TEXT NOT NULL,
                  snapshot TEXT NOT NULL, schema_version INTEGER NOT NULL,
                  graph_version TEXT NOT NULL, policy TEXT NOT NULL, status TEXT NOT NULL,
                  stop_reason TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL, used_attempts INTEGER NOT NULL DEFAULT 0,
                  created_at REAL NOT NULL, expires_at REAL NOT NULL, result TEXT,
                  owner TEXT, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT,
                  call_id TEXT, attempt_no INTEGER, kind TEXT NOT NULL, status TEXT,
                  input_hash TEXT, model TEXT, started_at REAL NOT NULL, ended_at REAL,
                  prompt_tokens INTEGER, completion_tokens INTEGER, detail TEXT);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, started_at);
                CREATE TABLE IF NOT EXISTS responses (
                  run_id TEXT NOT NULL, call_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                  response TEXT NOT NULL, PRIMARY KEY(run_id,call_id));
                CREATE TABLE IF NOT EXISTS steps (
                  run_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                  PRIMARY KEY(run_id,name));
            """)
            # 锁住检查与迁移，避免两个首次访问者同时添加同一列。
            db.execute("BEGIN IMMEDIATE")
            try:
                if "implementation_hash" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
                    db.execute("ALTER TABLE runs ADD COLUMN implementation_hash TEXT NOT NULL DEFAULT ''")
                db.commit()
            except Exception:
                db.rollback()
                raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def create(self, snapshot: dict, *, job_id=None, policy="hard-first-v1",
               max_attempts=12, ttl_seconds=86400, run_id=None) -> str:
        if policy not in POLICIES or type(max_attempts) is not int or max_attempts < 0:
            raise ValueError("Invalid policy or request budget")
        if ttl_seconds <= 0 or not snapshot.get("profiles"):
            raise ValueError("A complete planning snapshot and positive TTL are required")
        rid = run_id or uuid.uuid4().hex
        self._validate_id(rid)
        raw = canonical(snapshot)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO runs(run_id,job_id,input_hash,snapshot,schema_version,"
                       "graph_version,policy,status,max_attempts,created_at,expires_at,updated_at,implementation_hash)"
                       " VALUES(?,?,?,?,?,?,?,'ready',?,?,?,?,?)",
                       (rid, job_id, fingerprint(snapshot), raw, SCHEMA_VERSION, GRAPH_VERSION,
                        policy, max_attempts, now, now + ttl_seconds, now, implementation_hash()))
        return rid

    @staticmethod
    def _validate_id(rid):
        if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid):
            raise ValueError("Invalid planning run_id")

    def get(self, rid) -> dict:
        self._validate_id(rid)
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        out = dict(row)
        out["snapshot"] = json.loads(out["snapshot"])
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    def for_job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT run_id FROM runs WHERE job_id=?", (job_id,)).fetchone()
        return self.get(row[0]) if row else None

    def validate(self, rid):
        row = self.get(rid)
        if row["schema_version"] != SCHEMA_VERSION or row["graph_version"] != GRAPH_VERSION:
            raise RunError("运行版本不兼容，请基于资料重新建立run")
        if row["implementation_hash"] != implementation_hash():
            raise RunError("规划代码、模板或依赖版本已变化，请核对兼容性并基于资料新建run")
        if fingerprint(row["snapshot"]) != row["input_hash"]:
            raise RunError("规划输入快照指纹不一致，拒绝恢复")
        if row["expires_at"] < time.time():
            raise RunError("规划快照已过期，请重新核对资料并建立run")
        if row["cancel_requested"]:
            raise RunCancelled("规划已取消，不自动恢复")
        return row

    def update(self, rid, **fields):
        allowed = {"status", "stop_reason", "result", "owner"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Immutable snapshot/budget fields cannot be updated")
        if "result" in fields:
            fields["result"] = canonical(fields["result"])
        fields["updated_at"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE runs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE run_id=?",
                       (*fields.values(), rid))

    def guard(self, rid):
        if self.get(rid)["cancel_requested"]:
            raise RunCancelled("规划已取消")

    def cancel(self, rid):
        self.get(rid)
        with self.connect() as db:
            db.execute("UPDATE runs SET cancel_requested=1,updated_at=? WHERE run_id=?",
                       (time.time(), rid))

    @contextmanager
    def lease(self, rid):
        """OS文件锁在进程退出时释放；不凭TTL抢占仍在发请求的执行者。"""
        self._validate_id(rid)
        handle = (self.lock_dir / (rid + ".lock")).open("a+b")
        acquired = False
        try:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise RunBusy("同一规划正在执行，请等待当前执行者完成") from exc
            self.update(rid, owner=f"{os.getpid()}:{uuid.uuid4().hex}")
            # 此时无另一执行者；旧进程没有结算的外部调用不能推断为零消耗。
            with self.connect() as db:
                db.execute("UPDATE events SET status='unknown',ended_at=?,"
                           "detail='进程中断：外部请求结果未保存，可能重复'"
                           " WHERE run_id=? AND kind='http_attempt' AND status='in_flight'",
                           (time.time(), rid))
            yield
        finally:
            try:
                if acquired:
                    try:
                        self.update(rid, owner=None)
                    finally:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def event(self, rid, node, kind, detail="", status=None):
        with self.connect() as db:
            db.execute("INSERT INTO events(event_id,run_id,node_id,kind,status,started_at,detail)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, rid, node, kind, status, time.time(), detail))

    def reserve(self, rid, node, call_id, input_hash, model, retry_limit=3):
        """原子预留一笔实际SDK请求；SDK内部重试必须关闭。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(rid)
            if row["cancel_requested"]:
                db.rollback()
                raise RunCancelled("规划已取消")
            if row["used_attempts"] >= row["max_attempts"]:
                db.rollback()
                raise BudgetExhausted("规划实际请求预算耗尽")
            n = db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND call_id=?"
                           " AND kind='http_attempt'", (rid, call_id)).fetchone()[0]
            if n >= retry_limit:
                db.rollback()
                raise RunError("该模型调用已用尽重试次数；恢复不重置配额")
            eid = uuid.uuid4().hex
            db.execute("UPDATE runs SET used_attempts=used_attempts+1,updated_at=? WHERE run_id=?",
                       (time.time(), rid))
            db.execute("INSERT INTO events(event_id,run_id,node_id,call_id,attempt_no,kind,status,"
                       "input_hash,model,started_at) VALUES(?,?,?,?,?,'http_attempt','in_flight',?,?,?)",
                       (eid, rid, node, call_id, n + 1, input_hash, model, time.time()))
            db.commit()
        return eid

    def settle(self, eid, *, status, usage=None, detail="", response=None,
               rid=None, call_id=None, input_hash=None):
        p, c = None, None
        if usage is not None:
            if isinstance(usage, dict):
                p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
            else:
                p, c = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
        p = p if type(p) is int and p >= 0 else None
        c = c if type(c) is int and c >= 0 else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE events SET status=?,ended_at=?,prompt_tokens=?,completion_tokens=?,"
                       "detail=? WHERE event_id=?", (status, time.time(), p, c, detail, eid))
            if response is not None:
                db.execute("INSERT INTO responses(run_id,call_id,input_hash,response) VALUES(?,?,?,?)",
                           (rid, call_id, input_hash, canonical(response)))
            db.commit()

    def response(self, rid, call_id, input_hash):
        with self.connect() as db:
            row = db.execute("SELECT * FROM responses WHERE run_id=? AND call_id=?",
                             (rid, call_id)).fetchone()
        if row:
            if row["input_hash"] != input_hash:
                raise RunError("同一模型调用输入变更，拒绝复用；请新建run")
            return json.loads(row["response"])
        return None

    def put_step(self, rid, name, value):
        with self.connect() as db:
            db.execute("INSERT INTO steps VALUES(?,?,?) ON CONFLICT(run_id,name)"
                       " DO UPDATE SET value=excluded.value", (rid, name, canonical(value)))

    def step(self, rid, name):
        with self.connect() as db:
            row = db.execute("SELECT value FROM steps WHERE run_id=? AND name=?", (rid, name)).fetchone()
        return json.loads(row[0]) if row else None

    def events(self, rid):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id=?"
                                                " ORDER BY started_at,event_id", (rid,))]

    def summary(self, rid):
        row = self.get(rid)
        calls = [e for e in self.events(rid) if e["kind"] == "http_attempt"]
        return {"budget_scope": "planning", "max_http_attempts": row["max_attempts"],
                "http_attempts": row["used_attempts"],
                "semantic_calls": len({e["call_id"] for e in calls}),
                "sdk_retries": 0, "application_retries": sum(e["attempt_no"] > 1 for e in calls),
                "known_prompt_tokens": sum(e["prompt_tokens"] or 0 for e in calls),
                "known_completion_tokens": sum(e["completion_tokens"] or 0 for e in calls),
                "unknown_usage_attempts": sum(e["prompt_tokens"] is None or
                                              e["completion_tokens"] is None for e in calls),
                "unknown_result_attempts": sum(e["status"] in {"unknown", "in_flight"} for e in calls)}

    def public(self, rid):
        row = self.get(rid)
        reasons = []
        try:
            self.validate(rid)
        except RunError as exc:
            reasons.append(str(exc))
        post = self.step(rid, "postprocessing") or {}
        post_pending = bool(post) and any(str(v).startswith("failed:") for v in post.values())
        if post.get("index_written") == "skipped:no_embedding":
            from core import rag
            post_pending = post_pending or rag.available()
        if row["status"] in {"cancelled", "budget_exhausted", "fatal", "invalid_plan"} or (
                row["status"] == "done" and not post_pending):
            reasons.append(row["stop_reason"] or row["status"])
        # 余额为零仍可能从已保存响应/检查点完成纯节点；真正需要新请求时由reserve拒绝。
        if self.busy(rid):
            reasons.append("该run正在执行")
        if row["status"] == "failed":
            calls = {}
            for event in self.events(rid):
                if event["kind"] == "http_attempt":
                    calls.setdefault(event["call_id"], []).append(event)
            for cid, events in calls.items():
                if len(events) >= 3 and self.response(rid, cid, events[0]["input_hash"]) is None:
                    reasons.append("模型调用重试上限已耗尽，恢复不重置次数")
        return {k: row[k] for k in ("run_id", "job_id", "status", "stop_reason", "policy",
                                    "input_hash", "schema_version", "graph_version", "created_at",
                                    "expires_at", "cancel_requested")} | {
            "resumable": not reasons, "resume_blockers": reasons, "usage": self.summary(rid),
            "result": row["result"], "graph_complete": bool(self.step(rid, "graph_output")),
            "postprocessing": post, "latest_best": self.step(rid, "latest_best"),
            "latest_rejected": self.step(rid, "latest_rejected"),
            "node": self.step(rid, "current_node"), "implementation_hash": row["implementation_hash"]}

    def busy(self, rid):
        """仅探测OS锁，不修改owner、账本或取消状态。"""
        self._validate_id(rid)
        path = self.lock_dir / (rid + ".lock")
        with path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle, fcntl.LOCK_UN)
                return False
            except OSError:
                return True
