"""景点知识库（SQLite）：把每个景点深挖过的成果沉淀成资产。

- 同一景点在保鲜期内再次查询 -> 直接复用已采集数据，免重爬、免重复消耗；
- 报告生成后回填记录，形成"采集 -> 报告"的完整档案；
- 超过保鲜期（KB_TTL_DAYS，默认 7 天）视为过期，触发重新采集；
- 任务档案（jobs 表）：已完成/失败任务的终态落库，服务重启后历史不丢。
  运行中的任务只在内存（进程退出即视为中断）。

选 SQLite 而非 PostgreSQL：单机单用户场景零部署成本，将来上云再平滑迁移。
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR

_DB_PATH = DATA_DIR / "knowledge.db"

# 关键词归一化："西湖攻略"与"西湖"应命中同一缓存。去除常见后缀与空白；
# 归一化后为空则保留原串，避免全部归入同一个伪关键词。
_NOISE_SUFFIXES = ("旅游攻略", "攻略", "旅游", "旅行", "游玩", "怎么玩", "游记", "自由行")


def normalize_keyword(keyword: str) -> str:
    k = keyword.strip()
    for suf in _NOISE_SUFFIXES:
        if k.endswith(suf) and len(k) > len(suf):
            k = k[: -len(suf)]
            break
    k = re.sub(r"\s+", "", k)
    return k or keyword.strip()


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            keyword TEXT NOT NULL,
            mode TEXT,
            status TEXT NOT NULL,
            stage TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    # 城市->景点关联（行程/刷榜任务登记，刷榜时优先复用，免去重新圈定）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS heat_city (
            city TEXT NOT NULL,
            spot TEXT NOT NULL,
            PRIMARY KEY (city, spot)
        )
        """
    )
    # 热度快照：每城每对象一行（UPSERT），刷榜任务的产出，榜单页直接读；
    # kind 区分景点榜与美食榜（并列展示）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS heat_snapshots (
            city TEXT NOT NULL,
            spot TEXT NOT NULL,
            score REAL NOT NULL,
            fresh7 REAL DEFAULT 0,
            fresh60 REAL DEFAULT 0,
            old60 REAL DEFAULT 0,
            likes INTEGER DEFAULT 0,
            videos INTEGER DEFAULT 0,
            trend TEXT DEFAULT '平稳',
            kind TEXT DEFAULT '景点',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (city, spot)
        )
        """
    )
    try:  # 旧库迁移：早期版本无 kind 列，补上；新库创建时已含，此句报错被吞
        conn.execute("ALTER TABLE heat_snapshots ADD COLUMN kind TEXT DEFAULT '景点'")
    except sqlite3.OperationalError:
        pass
    # 报告登记表：行程等"无采集档案"的报告也在此登记，历史列表不遗漏。
    # 攻略报告仍随 spot_cache 登记（与采集缓存绑定），两处合并不重复。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            report_path TEXT NOT NULL,
            reported_at TEXT NOT NULL,
            video_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0
        )
        """
    )
    return conn


def record_crawl(keyword: str, raw_path: str, video_count: int, comment_count: int) -> int:
    """采集完成后登记一条知识库记录（关键词归一化后入库），返回记录 id。"""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO spot_cache (keyword, raw_path, video_count, comment_count, crawled_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (normalize_keyword(keyword), raw_path, video_count, comment_count,
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def update_report(record_id: int, report_path: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE spot_cache SET report_path = ?, reported_at = ? WHERE id = ?",
            (report_path, datetime.now().isoformat(timespec="seconds"), record_id),
        )


def find_fresh(keyword: str, ttl_days: int) -> dict | None:
    """返回该关键词保鲜期内的最新一条记录；过期或不存在返回 None。

    同时用归一化形式与原始形式查询：旧版本入库的记录未经归一化（如存的是"东湖游玩"），
    双形式兼容避免存量缓存失效。"""
    since = (datetime.now() - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    candidates = list(dict.fromkeys([normalize_keyword(keyword), keyword.strip()]))
    marks = ", ".join("?" for _ in candidates)
    with _conn() as conn:
        row = conn.execute(
            f"SELECT * FROM spot_cache WHERE keyword IN ({marks}) AND crawled_at >= ?"
            " ORDER BY crawled_at DESC LIMIT 1",
            (*candidates, since),
        ).fetchone()
    if not row:
        return None
    raw = Path(row["raw_path"])
    if not raw.exists():  # 缓存文件被清理则视为未命中
        return None
    return dict(row)


def stats() -> dict:
    """知识库概览：景点数、报告数（攻略 + 行程登记）、最近采集。"""
    with _conn() as conn:
        spots = conn.execute("SELECT COUNT(DISTINCT keyword) AS n FROM spot_cache").fetchone()["n"]
        reports = conn.execute("SELECT COUNT(*) AS n FROM spot_cache WHERE report_path IS NOT NULL").fetchone()["n"]
        reports += conn.execute("SELECT COUNT(*) AS n FROM report_registry").fetchone()["n"]
        latest = conn.execute("SELECT MAX(crawled_at) AS t FROM spot_cache").fetchone()["t"]
    return {"spots": spots, "reports": reports, "latest_crawl": latest}


def register_report(keyword: str, report_path: str, video_count: int = 0,
                    comment_count: int = 0) -> None:
    """登记一份无采集档案的报告（行程路书等），供历史列表展示。"""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO report_registry (keyword, report_path, reported_at,"
            " video_count, comment_count) VALUES (?, ?, ?, ?, ?)",
            (keyword, report_path, datetime.now().isoformat(timespec="seconds"),
             video_count, comment_count),
        )


def list_history(limit: int = 20) -> list[dict]:
    """最近的已生成报告列表（供网页历史卡片）：攻略报告（spot_cache）与
    行程报告（report_registry）合并，按生成时间降序；只返回文件仍存在的记录。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT keyword, video_count, comment_count, crawled_at, reported_at, report_path"
            " FROM spot_cache WHERE report_path IS NOT NULL"
            " ORDER BY reported_at DESC LIMIT ?",
            (limit * 2,),  # 多取一些，兼容文件被手动删除后的过滤
        ).fetchall()
        reg_rows = conn.execute(
            "SELECT keyword, video_count, comment_count, reported_at, report_path"
            " FROM report_registry ORDER BY reported_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        if not Path(r["report_path"]).exists():
            continue
        out.append(
            {
                "keyword": r["keyword"],
                "video_count": r["video_count"],
                "comment_count": r["comment_count"],
                "crawled_at": r["crawled_at"],
                "reported_at": r["reported_at"],
                "report_path": Path(r["report_path"]).name,
            }
        )
    for r in reg_rows:
        if not Path(r["report_path"]).exists():
            continue
        out.append(
            {
                "keyword": r["keyword"],
                "video_count": r["video_count"],
                "comment_count": r["comment_count"],
                "crawled_at": r["reported_at"],  # 行程报告无独立采集时间，以生成时间占位
                "reported_at": r["reported_at"],
                "report_path": Path(r["report_path"]).name,
            }
        )
    out.sort(key=lambda x: x["reported_at"] or "", reverse=True)
    return out[:limit]


# —— 任务档案：只持久化终态（完成/失败/取消），运行中任务仅存内存 ——

def record_job(job: dict) -> None:
    """任务到达终态时落库（UPSERT）：重启后历史任务仍可查询。"""
    result_json = json.dumps(job.get("result"), ensure_ascii=False) if job.get("result") else None
    with _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, keyword, mode, status, stage, result_json, error, created_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " status=excluded.status, stage=excluded.stage, result_json=excluded.result_json,"
            " error=excluded.error, finished_at=excluded.finished_at",
            (
                job["id"],
                job.get("keyword", ""),
                job.get("mode", ""),
                job.get("status", ""),
                job.get("stage", ""),
                result_json,
                job.get("error"),
                job.get("created_at", datetime.now().isoformat(timespec="seconds")),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def load_job(job_id: str) -> dict | None:
    """从库里还原一个终态任务（供内存未命中时查询）。"""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "keyword": row["keyword"],
        "mode": row["mode"],
        "status": row["status"],
        "stage": row["stage"],
        "log": [],  # 日志不持久化，重启后只保留结论
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
        "cache_hit": (json.loads(row["result_json"]).get("cache_hit")
                      if row["result_json"] else False),
        "finished_at": row["finished_at"],
    }


def list_jobs(limit: int = 20) -> list[dict]:
    """最近的终态任务摘要（供网页展示历史任务）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, keyword, mode, status, error, finished_at FROM jobs"
            " ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "keyword": r["keyword"],
            "mode": r["mode"],
            "status": r["status"],
            "error": r["error"],
            "finished_at": r["finished_at"],
        }
        for r in rows
    ]


# ---- 热度榜：城市景点关联 + 快照读写 ----

def register_city_spots(city: str, spots: list[str]) -> None:
    """登记城市->景点关联（行程/刷榜任务产出，供刷榜优先复用）。"""
    rows = [(city.strip(), s.strip()) for s in spots if s.strip()]
    if not rows:
        return
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO heat_city (city, spot) VALUES (?, ?)", rows)


def list_city_spots(city: str) -> list[str]:
    """已登记的城市景点清单（无则空列表）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT spot FROM heat_city WHERE city = ?", (city.strip(),)
        ).fetchall()
    return [r["spot"] for r in rows]


def upsert_heat_snapshot(city: str, spot: str, snap: dict, kind: str = "景点") -> None:
    """写入/更新某城某景点（或餐厅）的热度快照（每城每对象一行）。"""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO heat_snapshots (city, spot, score, fresh7, fresh60, old60,"
            " likes, videos, trend, kind, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(city, spot) DO UPDATE SET"
            " score=excluded.score, fresh7=excluded.fresh7, fresh60=excluded.fresh60,"
            " old60=excluded.old60, likes=excluded.likes, videos=excluded.videos,"
            " trend=excluded.trend, kind=excluded.kind, updated_at=excluded.updated_at",
            (city.strip(), spot.strip(), snap["score"], snap["fresh7"], snap["fresh60"],
             snap["old60"], snap["likes"], snap["videos"], snap["trend"], kind,
             datetime.now().isoformat(timespec="seconds")),
        )


def load_heat_snapshots(city: str) -> list[dict]:
    """某城的最新热度榜（按综合分降序，含 kind 供景点/美食分榜）；无快照返回空列表。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT spot, score, fresh7, fresh60, old60, likes, videos, trend, kind, updated_at"
            " FROM heat_snapshots WHERE city = ? ORDER BY score DESC",
            (city.strip(),),
        ).fetchall()
    return [
        {
            "spot": r["spot"], "score": r["score"], "trend": r["trend"],
            "fresh7": r["fresh7"], "fresh60": r["fresh60"], "old60": r["old60"],
            "likes": r["likes"], "videos": r["videos"],
            "kind": r["kind"] or "景点", "updated_at": r["updated_at"],
        }
        for r in rows
    ]
