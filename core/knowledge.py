"""景点知识库（SQLite）：把每个景点深挖过的成果沉淀成资产。

- 同一景点在保鲜期内再次查询 -> 直接复用已采集数据，免重爬、免重复消耗；
- 报告生成后回填记录，形成"采集 -> 报告"的完整档案；
- 超过保鲜期（KB_TTL_DAYS，默认 7 天）视为过期，触发重新采集。

选 SQLite 而非 PostgreSQL：单机单用户场景零部署成本，将来上云再平滑迁移。
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR

_DB_PATH = DATA_DIR / "knowledge.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spot_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            video_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            crawled_at TEXT NOT NULL,
            report_path TEXT,
            reported_at TEXT
        )
        """
    )
    return conn


def record_crawl(keyword: str, raw_path: str, video_count: int, comment_count: int) -> int:
    """采集完成后登记一条知识库记录，返回记录 id。"""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO spot_cache (keyword, raw_path, video_count, comment_count, crawled_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (keyword, raw_path, video_count, comment_count, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def update_report(record_id: int, report_path: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE spot_cache SET report_path = ?, reported_at = ? WHERE id = ?",
            (report_path, datetime.now().isoformat(timespec="seconds"), record_id),
        )


def find_fresh(keyword: str, ttl_days: int) -> dict | None:
    """返回该关键词保鲜期内的最新一条记录；过期或不存在返回 None。"""
    since = (datetime.now() - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM spot_cache WHERE keyword = ? AND crawled_at >= ?"
            " ORDER BY crawled_at DESC LIMIT 1",
            (keyword, since),
        ).fetchone()
    if not row:
        return None
    raw = Path(row["raw_path"])
    if not raw.exists():  # 缓存文件被清理则视为未命中
        return None
    return dict(row)


def stats() -> dict:
    """知识库概览：景点数、报告数、最近采集。"""
    with _conn() as conn:
        spots = conn.execute("SELECT COUNT(DISTINCT keyword) AS n FROM spot_cache").fetchone()["n"]
        reports = conn.execute("SELECT COUNT(*) AS n FROM spot_cache WHERE report_path IS NOT NULL").fetchone()["n"]
        latest = conn.execute("SELECT MAX(crawled_at) AS t FROM spot_cache").fetchone()["t"]
    return {"spots": spots, "reports": reports, "latest_crawl": latest}
