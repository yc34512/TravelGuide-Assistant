"""MySQL 报告版本适配。合同与 SQLiteReportStore 一致。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from core.storage.mysql_tasks import _engine
from config import MYSQL_DSN
from core.storage.reports import (ReportError, VersionConflict, VersionNotFound,
                                  content_hash, evidence_from_result, quality_status,
                                  _post_state)

DDL = [
    """CREATE TABLE IF NOT EXISTS reports (
      report_id VARCHAR(64) PRIMARY KEY,
      owner_id VARCHAR(128) NOT NULL,
      active_version_id VARCHAR(64) NULL,
      created_at DOUBLE NOT NULL,
      updated_at DOUBLE NOT NULL,
      KEY ix_reports_owner (owner_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS report_versions (
      version_id VARCHAR(64) PRIMARY KEY,
      report_id VARCHAR(64) NOT NULL,
      version_no INT NOT NULL,
      parent_version_id VARCHAR(64) NULL,
      job_id VARCHAR(32) NULL,
      owner_id VARCHAR(128) NOT NULL,
      input_snapshot_hash CHAR(64) NOT NULL,
      material_manifest LONGTEXT NOT NULL,
      policy_version VARCHAR(64) NOT NULL DEFAULT '',
      content_hash CHAR(64) NOT NULL,
      quality_status VARCHAR(32) NOT NULL,
      artifact_state VARCHAR(32) NOT NULL DEFAULT 'pending',
      index_state VARCHAR(64) NOT NULL DEFAULT 'pending',
      result_json LONGTEXT NOT NULL,
      change_summary TEXT NOT NULL,
      report_name VARCHAR(512) NOT NULL DEFAULT '',
      created_at DOUBLE NOT NULL,
      UNIQUE KEY ux_version_no (report_id, version_no),
      UNIQUE KEY ux_versions_job (job_id),
      KEY ix_versions_report (report_id, version_no),
      KEY ix_versions_report_name (report_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS report_evidence (
      version_id VARCHAR(64) NOT NULL,
      evidence_id VARCHAR(64) NOT NULL,
      spot VARCHAR(255) NULL,
      kind VARCHAR(32) NULL,
      source TEXT NULL,
      quote LONGTEXT NULL,
      as_of VARCHAR(64) NULL,
      meta LONGTEXT NOT NULL,
      PRIMARY KEY(version_id, evidence_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


class MySQLReportStore:
    def __init__(self, dsn: str | None = None):
        self.engine = _engine(dsn or MYSQL_DSN)
        with self.engine.begin() as conn:
            for statement in DDL:
                conn.exec_driver_sql(statement)
            try:
                conn.exec_driver_sql(
                    "ALTER TABLE report_versions ADD UNIQUE KEY ux_versions_job (job_id)")
            except Exception:
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
        from sqlalchemy import text
        result = result or {}
        now = time.time()
        vid = uuid.uuid4().hex
        chash = content_hash(result)
        qstatus = quality_status(result)
        artifact = "ready" if result.get("report_name") else "pending"
        index_state = _post_state(result, "index_written", "pending")
        material = {"report_name": result.get("report_name") or "",
                    "html_name": result.get("html_name") or "",
                    "planning_run_id": result.get("planning_run_id") or "",
                    "funnel": result.get("funnel") or {},
                    "request": request or {}}
        policy = str(((result.get("trip_plan") or {}).get("meta") or {})
                     .get("planning_policy") or "")
        if not input_snapshot_hash:
            input_snapshot_hash = hashlib.sha256(
                json.dumps({"report": result.get("report_name") or "", "job": job_id},
                           ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        with self.engine.begin() as conn:
            rid = report_id
            if rid is None:
                rid = uuid.uuid4().hex
                conn.execute(text(
                    "INSERT INTO reports(report_id,owner_id,active_version_id,created_at,updated_at)"
                    " VALUES(:id,:owner,NULL,:now,:now)"),
                    {"id": rid, "owner": owner_id, "now": now})
            else:
                report = conn.execute(text(
                    "SELECT * FROM reports WHERE report_id=:id FOR UPDATE"),
                    {"id": rid}).mappings().first()
                if report is None:
                    raise ReportError("报告不存在")
                if report["owner_id"] != owner_id:
                    raise ReportError("无权写入该报告")
            no = conn.execute(text(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM report_versions WHERE report_id=:id"),
                {"id": rid}).scalar_one()
            conn.execute(text(
                "INSERT INTO report_versions(version_id,report_id,version_no,parent_version_id,"
                "job_id,owner_id,input_snapshot_hash,material_manifest,policy_version,"
                "content_hash,quality_status,artifact_state,index_state,result_json,"
                "change_summary,report_name,created_at)"
                " VALUES(:vid,:rid,:no,:parent,:job,:owner,:ihash,:material,:policy,"
                ":chash,:quality,:artifact,:index_state,:result,:summary,:name,:now)"),
                {"vid": vid, "rid": rid, "no": no, "parent": parent_version_id,
                 "job": job_id, "owner": owner_id, "ihash": input_snapshot_hash,
                 "material": json.dumps(material, ensure_ascii=False, sort_keys=True),
                 "policy": policy, "chash": chash, "quality": qstatus,
                 "artifact": artifact, "index_state": index_state,
                 "result": json.dumps(result, ensure_ascii=False),
                 "summary": change_summary, "name": str(result.get("report_name") or ""),
                 "now": now})
            for ev in evidence_from_result(result):
                conn.execute(text(
                    "INSERT IGNORE INTO report_evidence(version_id,evidence_id,spot,kind,source,"
                    "quote,as_of,meta) VALUES(:v,:e,:s,:k,:src,:q,:a,:m)"),
                    {"v": vid, "e": ev["evidence_id"], "s": ev.get("spot"), "k": ev.get("kind"),
                     "src": ev.get("source"), "q": ev.get("quote"), "a": ev.get("as_of"),
                     "m": json.dumps(ev.get("meta") or {}, ensure_ascii=False)})
            if activate is None:
                activate = no == 1
            if activate:
                conn.execute(text(
                    "UPDATE reports SET active_version_id=:active,updated_at=:now"
                    " WHERE report_id=:id"), {"active": vid, "now": now, "id": rid})
            else:
                conn.execute(text("UPDATE reports SET updated_at=:now WHERE report_id=:id"),
                             {"now": now, "id": rid})
        return self.get_version(vid)

    def get_version(self, version_id: str) -> dict:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM report_versions WHERE version_id=:v"),
                               {"v": version_id}).mappings().first()
        out = self._version(row)
        if out is None:
            raise VersionNotFound(version_id)
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM report_evidence WHERE version_id=:v ORDER BY evidence_id"),
                {"v": version_id}).mappings().all()
        out["evidence"] = []
        for r in rows:
            item = dict(r)
            try:
                item["meta"] = json.loads(item.get("meta") or "{}")
            except (TypeError, ValueError):
                item["meta"] = {}
            out["evidence"].append(item)
        return out

    def get_report(self, report_id: str, owner_id: str | None = None) -> dict:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM reports WHERE report_id=:id"),
                               {"id": report_id}).mappings().first()
        if row is None or (owner_id is not None and row["owner_id"] != owner_id):
            raise VersionNotFound(report_id)
        out = dict(row)
        if out.get("active_version_id"):
            out["active_version"] = self.get_version(out["active_version_id"])
        return out

    def find_initial_version_by_job(self, job_id: str) -> dict | None:
        return self.find_version_by_job(job_id)

    def find_version_by_job(self, job_id: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT * FROM report_versions WHERE job_id=:j ORDER BY created_at DESC LIMIT 1"),
                {"j": job_id}).mappings().first()
        return self._version(row)

    def list_versions(self, report_id: str, owner_id: str | None = None) -> list[dict]:
        from sqlalchemy import text
        self.get_report(report_id, owner_id)
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM report_versions WHERE report_id=:r ORDER BY version_no"),
                {"r": report_id}).mappings().all()
        return [self._version(r) for r in rows]

    def activate(self, report_id: str, version_id: str, expected_active_version_id: str | None,
                owner_id: str | None = None) -> dict:
        from sqlalchemy import text
        now = time.time()
        with self.engine.begin() as conn:
            report = conn.execute(text(
                "SELECT * FROM reports WHERE report_id=:id FOR UPDATE"),
                {"id": report_id}).mappings().first()
            if report is None or (owner_id is not None and report["owner_id"] != owner_id):
                raise VersionNotFound(report_id)
            version = conn.execute(text(
                "SELECT * FROM report_versions WHERE version_id=:v AND report_id=:r"),
                {"v": version_id, "r": report_id}).mappings().first()
            if version is None:
                raise VersionNotFound(version_id)
            if (report["active_version_id"] or None) != (expected_active_version_id or None):
                raise VersionConflict("当前激活版本已变化，请刷新后再操作")
            if version["artifact_state"] != "ready":
                raise ReportError("该版本报告尚不可读，不能激活")
            conn.execute(text(
                "UPDATE reports SET active_version_id=:v,updated_at=:now WHERE report_id=:id"),
                {"v": version_id, "now": now, "id": report_id})
        return self.get_report(report_id, owner_id)

    def owner_of_job(self, job_id: str) -> str | None:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT owner_id FROM report_versions WHERE job_id=:j LIMIT 1"),
                {"j": job_id}).mappings().first()
        return row["owner_id"] if row else None

    def owner_of_report_name(self, report_name: str) -> str | None:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT owner_id FROM report_versions WHERE report_name=:n"
                " ORDER BY created_at DESC LIMIT 1"), {"n": report_name}).mappings().first()
        return row["owner_id"] if row else None

    def find_by_report_name(self, report_name: str, owner_id: str | None = None) -> dict | None:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT * FROM report_versions WHERE report_name=:n"
                " ORDER BY created_at DESC LIMIT 1"), {"n": report_name}).mappings().first()
        out = self._version(row)
        if out is None or (owner_id is not None and out["owner_id"] != owner_id):
            return None
        return out