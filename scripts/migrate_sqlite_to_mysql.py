"""SQLite -> MySQL 迁移 dry-run / 开发库导入。

默认只读盘点，不改任何源库：
    python scripts/migrate_sqlite_to_mysql.py
真正导入（仅显式 --execute，且必须给 --dsn）：
    python scripts/migrate_sqlite_to_mysql.py --execute --dsn mysql+pymysql://...

原则：
- 源库只读；不 drop/truncate；
- 目标表 IF NOT EXISTS；行按主键 INSERT IGNORE，可重入；
- 旧知识库数据明确归入 local_legacy 主体；
- checkpoint 文件不导入 MySQL（仍在固定 planning realm）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
SOURCES = {
    "knowledge": PROJECT / "data" / "knowledge.db",
    "tasks": PROJECT / "data" / "tasks.db",
    "reports": PROJECT / "data" / "reports.db",
    "planning_runs": PROJECT / "data" / "planning" / "runs.db",
}
# checkpoint 是图线程状态，不属于业务库迁移；明确排除。
SKIP_TABLES = {"sqlite_sequence", "checkpoints", "writes"}
LEGACY_OWNER = "local_legacy"


def _connect(path: Path):
    from core.sqlite_utils import ClosingConnection
    conn = sqlite3.connect(path, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def inventory(path: Path) -> dict:
    out = {"exists": path.exists(), "tables": {}}
    if not path.exists():
        return out
    with _connect(path) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            if name in SKIP_TABLES:
                continue
            cols = [dict(r) for r in conn.execute(f'PRAGMA table_info("{name}")')]
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out["tables"][name] = {
                "rows": count,
                "columns": [{"name": c["name"], "type": c["type"], "pk": c["pk"],
                             "notnull": bool(c["notnull"]), "default": c["dflt_value"]}
                            for c in cols],
                "primary_key": [c["name"] for c in sorted(cols, key=lambda x: x["pk"])
                                if c["pk"]],
            }
            if name == "vec_items":
                dims = [r[0] for r in conn.execute("SELECT DISTINCT dim FROM vec_items")]
                out["tables"][name]["distinct_dims"] = dims
                out["tables"][name]["collections"] = [
                    {"collection": r[0], "rows": r[1]} for r in conn.execute(
                        "SELECT collection, COUNT(*) FROM vec_items GROUP BY collection")]
    return out


def build_report() -> dict:
    report = {"sources": {}, "notes": [
        "dry-run 只读；未写入目标数据库",
        f"知识库旧数据归属：{LEGACY_OWNER}",
        "checkpoint 与 browser_profile 不迁移",
        "未知发布时间/费用/usage 保持 NULL，不补 0",
    ]}
    for name, path in SOURCES.items():
        report["sources"][name] = {"path": str(path), **inventory(path)}
    tasks = report["sources"].get("tasks", {}).get("tables", {})
    report["checks"] = {
        "task_jobs": tasks.get("task_jobs", {}).get("rows", 0),
        "job_steps": tasks.get("task_steps", {}).get("rows", 0),
        "task_events": tasks.get("task_events", {}).get("rows", 0),
        "legacy_jobs": report["sources"].get("knowledge", {})
        .get("tables", {}).get("jobs", {}).get("rows", 0),
        "vector_chunks": report["sources"].get("knowledge", {})
        .get("tables", {}).get("vec_items", {}).get("rows", 0),
        "report_versions": report["sources"].get("reports", {})
        .get("tables", {}).get("report_versions", {}).get("rows", 0),
    }
    return report


def _mysql_type(sqlite_type: str) -> str:
    t = (sqlite_type or "").upper()
    if "INT" in t:
        return "BIGINT"
    if any(x in t for x in ("REAL", "FLOA", "DOUB", "NUM")):
        return "DOUBLE"
    if "BLOB" in t:
        return "LONGBLOB"
    if any(x in t for x in ("CHAR", "CLOB", "TEXT")):
        return "LONGTEXT"
    return "VARCHAR(1024)"


def _create_table(conn, table: str, cols: list[dict], pk: list[str]) -> None:
    from sqlalchemy import text
    defs = []
    names = {c["name"] for c in cols}
    for c in cols:
        col = f'`{c["name"]}` {_mysql_type(c["type"])}'
        if c["pk"]:
            col += " NOT NULL"
        defs.append(col)
    if table not in ("task_jobs", "task_steps", "task_events", "report_versions",
                     "report_evidence", "reports", "principals", "resource_leases"):
        if "owner_id" not in names:
            defs.append(f"`owner_id` VARCHAR(128) NOT NULL DEFAULT '{LEGACY_OWNER}'")
    if pk:
        defs.append("PRIMARY KEY(" + ",".join(f"`{x}`" for x in pk) + ")")
    conn.execute(text(
        f"CREATE TABLE IF NOT EXISTS `{table}` (" + ",".join(defs) +
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))


def execute_import(dsn: str, report: dict) -> dict:
    from sqlalchemy import create_engine, text
    if dsn.startswith("mysql://"):
        dsn = "mysql+pymysql://" + dsn[len("mysql://"):]
    engine = create_engine(dsn, pool_pre_ping=True,
                           connect_args={"charset": "utf8mb4"})
    # 先让专用适配器建立任务/报告的正确 schema（索引、唯一键）。
    from core.storage.mysql_tasks import MySQLTaskStore
    from core.storage.mysql_reports import MySQLReportStore
    MySQLTaskStore(dsn)
    MySQLReportStore(dsn)
    results = {}
    for source, meta in report["sources"].items():
        path = Path(meta["path"])
        if not path.exists():
            continue
        results[source] = {}
        with _connect(path) as src:
            for table, info in meta["tables"].items():
                cols = info["columns"]
                pk = info["primary_key"]
                names = {c["name"] for c in cols}
                with engine.begin() as dst:
                    _create_table(dst, table, cols, pk)
                    if table not in ("task_jobs", "task_steps", "task_events",
                                     "report_versions", "report_evidence", "reports",
                                     "principals", "resource_leases") \
                            and "owner_id" not in names:
                        cols = cols + [{"name": "owner_id", "type": "TEXT", "pk": 0,
                                        "notnull": True, "default": LEGACY_OWNER}]
                    col_names = [c["name"] for c in cols]
                    placeholders = ",".join(":" + c for c in col_names)
                    sql = text(f"INSERT IGNORE INTO `{table}` (" +
                               ",".join(f"`{c}`" for c in col_names) +
                               ") VALUES (" + placeholders + ")")
                    rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
                    batch = []
                    for row in rows:
                        item = {c: row[c] if c in row.keys() else LEGACY_OWNER
                                for c in col_names}
                        batch.append(item)
                        if len(batch) >= 500:
                            dst.execute(sql, batch)
                            batch = []
                    if batch:
                        dst.execute(sql, batch)
                    count = dst.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
                results[source][table] = {"source_rows": info["rows"], "target_rows": count}
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SQLite -> MySQL 迁移（默认 dry-run）")
    parser.add_argument("--execute", action="store_true", help="真正导入开发库")
    parser.add_argument("--dsn", default="", help="MYSQL_DSN（执行导入时必填）")
    parser.add_argument("--out", default=str(PROJECT / "data" / "upgrade"
                                             / "migration_dry_run.json"))
    args = parser.parse_args(argv)
    report = build_report()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not args.execute:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"dry-run 完成：{out}")
        print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
        print("未写入任何目标库。真实导入需显式 --execute --dsn。")
        return 0
    if not args.dsn:
        print("--execute 需要 --dsn；拒绝执行")
        return 2
    results = execute_import(args.dsn, report)
    # 对账：记录源/目标行数；不自动删除目标多余行（需人工确认）。
    report["import_results"] = results
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    mismatches = {s: {t: v for t, v in tables.items() if v["target_rows"] < v["source_rows"]}
                  for s, tables in results.items()}
    print(f"导入完成：{out}")
    print(json.dumps(mismatches, ensure_ascii=False, indent=2))
    return 0 if not any(mismatches.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())