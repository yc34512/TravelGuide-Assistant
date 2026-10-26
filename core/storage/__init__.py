"""存储访问层：任务、报告版本与容量租约。

本包对外只暴露接口工厂；具体实现按 STORAGE_BACKEND 选择。默认 SQLite 本地模式，
MySQL 适配在 core.storage.mysql 中，缺少驱动或连接失败时明确报错，不偷偷回退。
"""
from __future__ import annotations

from config import STORAGE_BACKEND


def task_store(path=None):
    if STORAGE_BACKEND == "mysql":
        from core.storage.mysql_tasks import MySQLTaskStore
        return MySQLTaskStore()
    from core.storage.tasks import SQLiteTaskStore
    return SQLiteTaskStore(path)


def report_store(path=None):
    if STORAGE_BACKEND == "mysql":
        from core.storage.mysql_reports import MySQLReportStore
        return MySQLReportStore()
    from core.storage.reports import SQLiteReportStore
    return SQLiteReportStore(path)