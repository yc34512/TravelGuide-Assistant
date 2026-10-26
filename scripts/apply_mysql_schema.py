"""创建 MySQL 任务/报告 schema（幂等）。需要 MYSQL_DSN 或 --dsn。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from core.storage.mysql_reports import MySQLReportStore
from core.storage.mysql_tasks import MySQLTaskStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="创建 TravelGuide MySQL schema")
    parser.add_argument("--dsn", default="", help="缺省读取 MYSQL_DSN")
    args = parser.parse_args(argv)
    MySQLTaskStore(args.dsn or None)
    MySQLReportStore(args.dsn or None)
    print("task/report schema 已创建（IF NOT EXISTS，未修改既有数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
