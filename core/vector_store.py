"""向量存储组件：SQLite 为唯一真源，faiss 为可选查询加速。

设计依据（实现规格 §12.5 / §13.5 / §12.6）：
- 选型：faiss-cpu + 向量与元数据存 SQLite（轻、纯本地无外部服务、Windows wheel 成熟）；
- §12 与 §13 的索引**分开建、不得混用**：用 collection 隔离——
  "entity"（点位名称，实体判定用）与 "corpus"（文本片段，语义召回用）；
- 降级：无 faiss 环境（如离线测试）→ SQLite 读向量 + 纯 Python 余弦。
  本地语料规模（几百~几千条）下两种实现结果一致，faiss 只影响查询实现、不影响正确性。

向量以标准库 array('f') 存 BLOB：faiss 缺席时也不引入 numpy 依赖。
数据落在 knowledge.db 同一文件（测试替换 core.knowledge._DB_PATH 即可全部隔离）。
"""
from __future__ import annotations

import json
import sqlite3
from array import array
from datetime import datetime
from core.sqlite_utils import ClosingConnection

_LOCK_HINT = None   # 并发写由 SQLite 自身事务保证；本模块不做进程内缓存（查询频率低）


def _conn() -> sqlite3.Connection:
    from core import knowledge   # 延迟导入：复用同一个 DB 路径变量（测试可替换）
    conn = sqlite3.connect(knowledge._DB_PATH, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vec_items (
            collection TEXT NOT NULL,
            item_key   TEXT NOT NULL,
            text       TEXT NOT NULL,
            vec        BLOB NOT NULL,
            dim        INTEGER NOT NULL,
            meta       TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY (collection, item_key)
        )
        """
    )
    return conn


def pack_vec(vec: list[float]) -> bytes:
    return array("f", [float(x) for x in vec]).tobytes()


def unpack_vec(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(bytes(blob))
    return list(a)


def upsert_items(collection: str, items: list[dict]) -> int:
    """批量写入/更新向量条目。items: [{key, text, vec, meta?}]，返回写入条数。

    只写不删：同一 key 重复写入走 UPSERT（如点位名重新采集后刷新向量）。
    vec 为空的条目跳过（宁缺勿滥，不存半成品）。"""
    rows = []
    now = datetime.now().isoformat(timespec="seconds")
    for it in items or []:
        key = str(it.get("key") or "").strip()
        vec = it.get("vec") or []
        text = str(it.get("text") or "").strip()
        if not key or not vec or not text:
            continue
        meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
        rows.append((collection, key, text, pack_vec(vec), len(vec),
                     json.dumps(meta, ensure_ascii=False), now))
    if not rows:
        return 0
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO vec_items (collection, item_key, text, vec, dim, meta, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(collection, item_key) DO UPDATE SET"
            " text=excluded.text, vec=excluded.vec, dim=excluded.dim,"
            " meta=excluded.meta, created_at=excluded.created_at",
            rows,
        )
    return len(rows)


def get_item(collection: str, key: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM vec_items WHERE collection = ? AND item_key = ?",
            (collection, key),
        ).fetchone()
    return _row_to_dict(row) if row else None


def count(collection: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM vec_items WHERE collection = ?", (collection,)
        ).fetchone()
    return row["n"]


def keys(collection: str) -> set[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT item_key FROM vec_items WHERE collection = ?", (collection,)
        ).fetchall()
    return {r["item_key"] for r in rows}


def clear(collection: str) -> int:
    """清空某 collection（重建索引/测试用）。"""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM vec_items WHERE collection = ?", (collection,))
        return cur.rowcount


def has_key_prefix(collection: str, prefix: str) -> bool:
    """是否存在以 prefix 开头的条目（用于"这份档案/这条攻略是否已入库"的幂等判断）。"""
    if not prefix:
        return False
    esc = (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM vec_items WHERE collection = ? AND item_key LIKE ? ESCAPE '\\' LIMIT 1",
            (collection, esc),
        ).fetchone()
    return row is not None


def delete_prefix(collection: str, prefix: str) -> int:
    """删除以 prefix 开头的条目（重建某份档案的索引前先清旧版，避免陈旧片段残留）。"""
    if not prefix:
        return 0
    esc = (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM vec_items WHERE collection = ? AND item_key LIKE ? ESCAPE '\\'",
            (collection, esc),
        )
        return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict:
    try:
        meta = json.loads(row["meta"] or "{}")
    except (ValueError, TypeError):
        meta = {}
    return {
        "key": row["item_key"], "text": row["text"], "dim": row["dim"],
        "meta": meta if isinstance(meta, dict) else {},
        "vec": unpack_vec(row["vec"]),
        "created_at": row["created_at"],
    }


def _cosine_scores_python(query: list[float], vecs: list[list[float]]) -> list[float | None]:
    """纯 Python 余弦（归一化后点积）。维度不符/零向量返回 None（不参与排序）。"""
    qn = _normalize(query)
    if not qn:
        return [None] * len(vecs)
    out: list[float | None] = []
    for v in vecs:
        vn = _normalize(v)
        if len(vn) != len(qn):
            out.append(None)
            continue
        out.append(sum(a * b for a, b in zip(qn, vn)))
    return out


def _normalize(v: list[float]) -> list[float]:
    try:
        s = sum(float(x) * float(x) for x in v) ** 0.5
    except (TypeError, ValueError):
        return []
    if s <= 0:
        return []
    return [float(x) / s for x in v]


def _cosine_scores_faiss(query: list[float], vecs: list[list[float]]) -> list[float] | None:
    """faiss 实现（IndexFlatIP + L2 归一化 = 余弦）；环境不可用返回 None 交回降级。"""
    try:
        import faiss
        import numpy as np
    except Exception:
        return None
    try:
        arr = np.asarray(vecs, dtype="float32")
        if arr.ndim != 2 or arr.shape[1] != len(query):
            return None
        faiss.normalize_L2(arr)
        q = np.asarray([query], dtype="float32")
        faiss.normalize_L2(q)
        index = faiss.IndexFlatIP(arr.shape[1])
        index.add(arr)
        sims, _ = index.search(q, arr.shape[0])
        return [float(x) for x in sims[0]]
    except Exception:
        return None


def search(collection: str, query_vec: list[float], top_k: int = 5) -> list[dict]:
    """余弦 Top-K 检索。返回 [{key, text, meta, score}]，按相似度降序。

    无效条目（维度不符/零向量）跳过，不猜；负余弦（语义相反）不构成召回，同样排除。
    空集合返回空列表。"""
    if not query_vec:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM vec_items WHERE collection = ?", (collection,)
        ).fetchall()
    items = [_row_to_dict(r) for r in rows]
    if not items:
        return []
    vecs = [it["vec"] for it in items]
    scores = _cosine_scores_faiss(query_vec, vecs)
    if scores is None:   # 无 faiss（离线测试）→ 纯 Python 余弦（结果一致）
        scores = _cosine_scores_python(query_vec, vecs)
    ranked = [(it, sc) for it, sc in zip(items, scores) if sc is not None and sc >= 0]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [{"key": it["key"], "text": it["text"], "meta": it["meta"],
             "score": round(float(sc), 4)}
            for it, sc in ranked[: max(1, int(top_k or 1))]]
