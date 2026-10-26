# Schema 迁移

服务模式的 MySQL schema 由两个适配器幂等创建：

    python scripts/apply_mysql_schema.py --dsn mysql+pymysql://user:pass@127.0.0.1/travelguide

- `core/storage/mysql_tasks.py`：task_jobs、task_steps、task_events、principals、resource_leases。
- `core/storage/mysql_reports.py`：reports、report_versions、report_evidence。

业务知识库（spot_cache、heat、候选、别名、vec_items、planning runs 等）用
`python scripts/migrate_sqlite_to_mysql.py --execute --dsn ...` 导入；默认 dry-run。

本轮没有引入 Alembic 版本链：schema 仍处于开发期，适配器按 `CREATE TABLE IF NOT EXISTS`
建表。正式多环境发布前应把 DDL 固化到 Alembic revision，并停止直接建表。
