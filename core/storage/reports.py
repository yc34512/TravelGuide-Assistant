"""报告与不可变版本账本（SQLite 本地模式）。

旧报告不被原地覆盖：每次变更生成新 version，旧版保留完整 result 与证据。
首版初次报告在文件可读后自动成为 active；变更版本必须由用户显式激活（CAS）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from config import REPORT_DB_PATH
from core.sqlite_utils import ClosingConnection


class ReportError(RuntimeError):
    pass


class VersionNotFound(ReportError):
    pass


class VersionConflict(ReportError):
    pass


def content_hash(result: dict) -> str:
    material = result.get("markdown") or json.dumps(result.get("trip_plan") or {},
                                                    ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def quality_status(result: dict) -> str:
    quality = ((result or {}).get("quality")
               or (((result or {}).get("trip_plan") or {}).get("quality")) or {})
    unresolved = quality.get("unresolved") or []
    passed = quality.get("passed", True)
    return "pass" if passed and not unresolved else "warning"


def _post_state(result: dict, key: str, default: str) -> str:
    post = (result or {}).get("postprocessing") or {}
    return str(post.get(key) or default)


def evidence_from_result(result: dict) -> list[dict]:
    """从已完成结果抽取**原文证据**；不把 LLM 摘要冒充评论原话。"""
    tp = (result or {}).get("trip_plan") or {}
    snap = tp.get("snap") or {}
    as_of = (tp.get("meta") or {}).get("facts_cutoff") or ""
    out: dict[str, dict] = {}

    def add(kind: str, source: str, quote: str, spot: str = "", meta: dict | None = None,
            eid: str = "") -> None:
        quote = str(quote or "").strip()
        source = str(source or "").strip()
        if not quote or not source:
            return
        stable = eid or hashlib.sha256(
            ("|".join([kind, spot, source, quote])).encode("utf-8")).hexdigest()[:24]
        out.setdefault(stable, {
            "evidence_id": stable, "spot": spot, "kind": kind, "source": source,
            "quote": quote, "as_of": as_of, "meta": meta or {},
        })

    for row in snap.get("pitfall") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("support_status") or "") == "pending":
            continue
        add("pitfall", row.get("source") or "", row.get("quote") or "",
            meta={"claim": row.get("claim") or "", "confidence": row.get("confidence") or ""},
            eid=str(row.get("evidence_id") or ""))
    for name, dg in (snap.get("digests") or {}).items():
        if not isinstance(dg, dict):
            continue
        points = {p.get("quote_id"): p for p in (dg.get("quote_points") or [])
                  if isinstance(p, dict)}
        for qid in dg.get("quote_ids") or []:
            p = points.get(qid) or {}
            e = p.get("evidence") or {}
            add("comment", p.get("source") or e.get("source") or "",
                p.get("quote") or e.get("excerpt") or "", spot=str(name),
                meta={"quote_id": qid}, eid=str(p.get("evidence_id") or ""))
    for bucket in ("poi", "food"):
        for name, det in ((tp.get("catalog") or {}).get(bucket) or {}).items():
            if not isinstance(det, dict):
                continue
            official = det.get("official") or {}
            url = official.get("source_url") or ""
            if not url:
                continue
            bits = []
            if det.get("ticket_price") is not None:
                bits.append(f"票价 {det['ticket_price']} 元")
            if det.get("open_hours"):
                bits.append(f"开放时间 {det['open_hours']}")
            if det.get("close_day"):
                bits.append(f"闭馆日 {det['close_day']}")
            add("official", url, "；".join(bits), spot=str(name),
                meta={"official_name": official.get("name") or ""})
    return list(out.values())


class SQLiteReportStore:
    def __init__(self, path=None):
        self.path = Path(path or REPORT_DB_PATH)
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
                CREATE TABLE IF NOT EXISTS reports (
                  report_id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL,
                  active_version_id TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_reports_owner
                  ON reports(owner_id, created_at);
                CREATE TABLE IF NOT EXISTS report_versions (
                  version_id TEXT PRIMARY KEY,
                  report_id TEXT NOT NULL,
                  version_no INTEGER NOT NULL,
                  parent_version_id TEXT,
                  job_id TEXT,
                  owner_id TEXT NOT NULL,
                  input_snapshot_hash TEXT NOT NULL,
                  material_manifest TEXT NOT NULL DEFAULT '{}',
                  policy_version TEXT NOT NULL DEFAULT '',
                  content_hash TEXT NOT NULL,
                  quality_status TEXT NOT NULL,
                  artifact_state TEXT NOT NULL DEFAULT 'pending',
                  index_state TEXT NOT NULL DEFAULT 'pending',
                  result_json TEXT NOT NULL,
                  change_summary TEXT NOT NULL DEFAULT '',
                  report_name TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL,
                  UNIQUE(report_id, version_no)
                );
                CREATE INDEX IF NOT EXISTS ix_versions_report
                  ON report_versions(report_id, version_no);
                CREATE INDEX IF NOT EXISTS ix_versions_report_name
                  ON report_versions(report_name);
                CREATE TABLE IF NOT EXISTS report_evidence (
                  version_id TEXT NOT NULL,
                  evidence_id TEXT NOT NULL,
                  spot TEXT,
                  kind TEXT,
                  source TEXT,
                  quote TEXT,
                  as_of TEXT,
                  meta TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(version_id, evidence_id)
                );
            """)
            try:
                conn.execute("ALTER TABLE report_versions ADD COLUMN report_name TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_versions_job"
                             " ON report_versions(job_id) WHERE job_id IS NOT NULL")
            except sqlite3.IntegrityError:
                # 历史库可能已有同一 job 的重复版本；不删数据，保留人工对账入口。
                pass

    @staticmethod
    def _version(row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("result_json", "material_manifest"):
            if out.get(key):
                try:
                    out[key] = json.loads(out[key])
                except (TypeError, ValueError):
                    pass
        out["result"] = out.get("result_json")
        return out

    def register_result(self, *, job_id: str, owner_id: str, result: dict,
                        report_id: str | None = None,
                        parent_version_id: str | None = None,
                        change_summary: str = "",
                        activate: bool | None = None,
                        input_snapshot_hash: str = "",
                        request: dict | None = None) -> dict:
        result = result or {}
        now = time.time()
        vid = uuid.uuid4().hex
        chash = content_hash(result)
        qstatus = quality_status(result)
        artifact = "ready" if result.get("report_name") else "pending"
        index_state = _post_state(result, "index_written", "pending")
        material = {
            "report_name": result.get("report_name") or "",
            "html_name": result.get("html_name") or "",
            "planning_run_id": result.get("planning_run_id") or "",
            "funnel": result.get("funnel") or {},
            "request": request or {},
        }
        policy = str(((result.get("trip_plan") or {}).get("meta") or {})
                     .get("planning_policy") or "")
        if not input_snapshot_hash:
            input_snapshot_hash = hashlib.sha256(
                json.dumps({"report": result.get("report_name") or "",
                            "job": job_id}, ensure_ascii=False,
                           sort_keys=True).encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rid = report_id
                if rid is None:
                    rid = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO reports(report_id,owner_id,active_version_id,created_at,updated_at)"
                        " VALUES(?,?,NULL,?,?)", (rid, owner_id, now, now))
                else:
                    row = conn.execute("SELECT * FROM reports WHERE report_id=?",
                                       (rid,)).fetchone()
                    if row is None:
                        raise ReportError("报告不存在")
                    if row["owner_id"] != owner_id:
                        raise ReportError("无权写入该报告")
                no = conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 FROM report_versions WHERE report_id=?",
                    (rid,)).fetchone()[0]
                conn.execute(
                    "INSERT INTO report_versions(version_id,report_id,version_no,parent_version_id,"
                    "job_id,owner_id,input_snapshot_hash,material_manifest,policy_version,"
                    "content_hash,quality_status,artifact_state,index_state,result_json,"
                    "change_summary,report_name,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (vid, rid, no, parent_version_id, job_id, owner_id, input_snapshot_hash,
                     json.dumps(material, ensure_ascii=False, sort_keys=True), policy,
                     chash, qstatus, artifact, index_state,
                     json.dumps(result, ensure_ascii=False), change_summary,
                     str(result.get("report_name") or ""), now))
                for ev in evidence_from_result(result):
                    conn.execute(
                        "INSERT INTO report_evidence(version_id,evidence_id,spot,kind,source,"
                        "quote,as_of,meta) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(version_id,evidence_id)"
                        " DO NOTHING",
                        (vid, ev["evidence_id"], ev.get("spot"), ev.get("kind"),
                         ev.get("source"), ev.get("quote"), ev.get("as_of"),
                         json.dumps(ev.get("meta") or {}, ensure_ascii=False)))
                if activate is None:
                    activate = no == 1
                if activate:
                    conn.execute("UPDATE reports SET active_version_id=?,updated_at=?"
                                 " WHERE report_id=?", (vid, now, rid))
                else:
                    conn.execute("UPDATE reports SET updated_at=? WHERE report_id=?",
                                 (now, rid))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_version(vid)

    def get_version(self, version_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM report_versions WHERE version_id=?",
                               (version_id,)).fetchone()
        out = self._version(row)
        if out is None:
            raise VersionNotFound(version_id)
        with self._connect() as conn:
            ev = conn.execute(
                "SELECT * FROM report_evidence WHERE version_id=? ORDER BY evidence_id",
                (version_id,)).fetchall()
        out["evidence"] = []
        for r in ev:
            item = dict(r)
            try:
                item["meta"] = json.loads(item.get("meta") or "{}")
            except (TypeError, ValueError):
                item["meta"] = {}
            out["evidence"].append(item)
        return out

    def get_report(self, report_id: str, owner_id: str | None = None) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None or (owner_id is not None and row["owner_id"] != owner_id):
            raise VersionNotFound(report_id)
        out = dict(row)
        if out.get("active_version_id"):
            out["active_version"] = self.get_version(out["active_version_id"])
        return out

    def find_initial_version_by_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM report_versions WHERE job_id=? ORDER BY created_at LIMIT 1",
                (job_id,)).fetchone()
        return self._version(row)

    def find_version_by_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM report_versions WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                (job_id,)).fetchone()
        return self._version(row)

    def list_versions(self, report_id: str, owner_id: str | None = None) -> list[dict]:
        self.get_report(report_id, owner_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM report_versions WHERE report_id=? ORDER BY version_no",
                (report_id,)).fetchall()
        return [self._version(r) for r in rows]

    def activate(self, report_id: str, version_id: str, expected_active_version_id: str | None,
                owner_id: str | None = None) -> dict:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                report = conn.execute("SELECT * FROM reports WHERE report_id=?",
                                      (report_id,)).fetchone()
                if report is None or (owner_id is not None and report["owner_id"] != owner_id):
                    raise VersionNotFound(report_id)
                version = conn.execute(
                    "SELECT * FROM report_versions WHERE version_id=? AND report_id=?",
                    (version_id, report_id)).fetchone()
                if version is None:
                    raise VersionNotFound(version_id)
                current = report["active_version_id"]
                if (current or None) != (expected_active_version_id or None):
                    raise VersionConflict("当前激活版本已变化，请刷新后再操作")
                if version["artifact_state"] != "ready":
                    raise ReportError("该版本报告尚不可读，不能激活")
                conn.execute("UPDATE reports SET active_version_id=?,updated_at=?"
                             " WHERE report_id=?", (version_id, now, report_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_report(report_id, owner_id)

    def owner_of_job(self, job_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT owner_id FROM report_versions WHERE job_id=? LIMIT 1",
                               (job_id,)).fetchone()
        return row["owner_id"] if row else None

    def owner_of_report_name(self, report_name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM report_versions WHERE report_name=?"
                " ORDER BY created_at DESC LIMIT 1", (report_name,)).fetchone()
        return row["owner_id"] if row else None

    def find_by_report_name(self, report_name: str, owner_id: str | None = None) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM report_versions WHERE report_name=?"
                " ORDER BY created_at DESC LIMIT 1", (report_name,)).fetchone()
        out = self._version(row)
        if out is None or (owner_id is not None and out["owner_id"] != owner_id):
            return None
        return out