"""§12 实体匹配层：向量召回 + 一道确认判决（D17 / D2 / D3）。

为什么需要它（设计规格 §12.1 / §12.2）：
- 漏命中良性（多采一次，只花成本不伤正确性、用户无感）；
- 误命中是事故（静默用错点位的数据，用户完全看不出来）；
- "故宫/紫禁城"（同一实体）与"宽窄巷子/锦里"（不同实体）都是零字符重叠、
  语义相邻，**两组相似度分布是重叠的**——单一阈值必然同时产生两类错误。
  所以问题不在阈值怎么调，而在于"判决不能只靠相似度这一个数"。

三级判定（§4.2，必须分级、不得用单一阈值；中档绝不自动替换——静默换掉
用户要去的点位是本设计最严重的事故类型）：
    高 = 同一实体（关键词路命中 / 别名词典命中 / LLM 判决"是"）→ 用历史记录
    中 = 疑似同一（向量召回但判决不可用/判不准）→ 不进 reused，记录"疑似命中 X"供排查
    低 = 新实体（无召回 / LLM 判"否"）→ 正常处理

硬性要求（§12.3）：
1. 主力仍是精确 + 前缀匹配（沿用 find_fresh），语义检索只做兜底；
2. 禁止出现"相似度 ≥ 某值 → 判同一实体"这类写法——向量路的输出只能进判决层做候选；
3. LLM 判决必须批量（一次判多个），不得逐个调用；
4. 每一次复用都要能回溯：命中谁、走的哪条路、相似度多少、最终谁拍的板
   ——全部写进 intermediate（semantic_hits 表）。

写入时机（D4）：向量索引与 alias_map 只允许在**采集验证通过之后**写——
register_entity() 只由"现场采集成功"的调用点触发，禁止"LLM 提到过就入库"
（那会把编造的点位固化成可信历史）。

降级链（§12.6，必须实现）：
    完整（混合检索 + 判决）
      → 无 embedding / 无网络：跳过向量路，只走关键词路 + 别名词典 + 精确匹配
      → 无 faiss（离线测试）：SQLite 读向量 + 纯 Python 余弦（vector_store 内部）
      → 全不可用：退化为现有 find_fresh 行为（零回归）
"""
from __future__ import annotations

from core import embeddings, knowledge, vector_store
from core.llm import chat_json

# §12.5：§12 的索引是"点位名称"，与 §13 的"文本片段"索引分开建、不得混用
ENTITY_COLLECTION = "entity"
# 召回可以宽松（宁可多召回，不许漏）：每名最多召回 3 个候选
_RECALL_TOP_K = 3
# 判决必须严格：每名最多送判 2 个候选（判太多把成本推高，且 top-1 已覆盖绝大多数别名场景）
_CONFIRM_PER_NAME = 2
# "任何历史"的近拟：find_fresh 的 ttl 用超大值即不限保鲜期（不改 find_fresh 本身的行为）
_ANY_TTL_DAYS = 36500

_CONFIRM_SYSTEM = """你是地点实体判定专家。逐组判断两个名称是否指向**同一个具体地点**。

判断标准：
1. 同一景点的别名/旧称/俗称 -> 同一（如"故宫"与"紫禁城"）；
2. 同一城市内相邻但不同的两个点位 -> 不是同一（如"宽窄巷子"与"锦里"）；
3. 同一品牌的不同分店 -> 不是同一（如"四季民福（故宫店）"与"四季民福（前门店）"）；
4. 一个名称是另一个的无歧义简称 -> 同一（如"黄鹤楼"与"黄鹤楼公园"）；
5. 只有高度确信才答 "yes"；有任何不确定一律答 "unsure"，禁止联想、推测、脑补。

输出严格 JSON：{"results": [{"q": "名称1", "c": "名称2", "same": "yes|no|unsure", "reason": "一句话依据"}]}
（q/c 必须原样照抄输入，不得改写）"""

# 惰性回填标记（进程级）：entity 索引首次使用时从 spot_cache 同步历史健康记录
_BACKFILL_DONE = False


def reset_index_state() -> None:
    """重置惰性回填标记（测试用：换临时库后需重新回填）。"""
    global _BACKFILL_DONE
    _BACKFILL_DONE = False


def _try_fresh(name: str, ttl_days: int) -> dict | None:
    """find_fresh 防御包装：库异常时视为未命中（绝不中断主流程）。"""
    try:
        return knowledge.find_fresh(name, ttl_days)
    except Exception:
        return None


def _keyword_via(query: str, hit_keyword: str) -> str:
    """关键词路命中细分：归一化后完全相同 = exact，否则 = prefix（仅日志用）。"""
    try:
        n = knowledge.normalize_keyword(query)
        h = knowledge.normalize_keyword(hit_keyword or "")
        return "exact" if h and n == h else "prefix"
    except Exception:
        return "keyword"


def _pack(record: dict | None, level: str, via: str, hit_name: str | None,
          score: float | None, expired: bool, detail: str) -> dict:
    """统一返回结构。record 仅在"高 + 命中新鲜记录"时非空（供调用方直接复用）。"""
    return {"record": record, "level": level, "via": via, "hit_name": hit_name,
            "score": score, "expired": bool(expired), "detail": detail}


def _log_hit(query: str, hit: str | None, score: float | None, level: str,
             via: str, adopted: bool, note: str) -> None:
    try:
        knowledge.record_semantic_hit(query, hit, score, level, via, adopted, note)
    except Exception:
        pass   # 日志写失败不影响判定本身


def _pair_key(q: str, c: str, pairs: list[tuple[str, str]]) -> tuple[str, str] | None:
    """把 LLM 返回的名称对映射回原对（容忍标点/空白微调：精确 -> 归一化）。"""
    for pq, pc in pairs:
        if q == pq and c == pc:
            return (pq, pc)
    nq, nc = knowledge.normalize_keyword(q), knowledge.normalize_keyword(c)
    for pq, pc in pairs:
        if (knowledge.normalize_keyword(pq) == nq
                and knowledge.normalize_keyword(pc) == nc):
            return (pq, pc)
    return None


def _confirm_entities(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """批量判决：一次 LLM 调用判多组（§12.3 硬性要求 2）。

    LLM 不可用/输出不合法时返回空 dict——调用方按"判不准"处理（进中档，不采用）。"""
    if not pairs:
        return {}
    lines = [f'{i}. q="{q}"，c="{c}"' for i, (q, c) in enumerate(pairs, 1)]
    try:
        data = chat_json(_CONFIRM_SYSTEM, "名称对：\n" + "\n".join(lines))
    except Exception:
        return {}
    out: dict[tuple[str, str], str] = {}
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        q = str(r.get("q") or "").strip()
        c = str(r.get("c") or "").strip()
        verdict = str(r.get("same") or "").strip().lower()
        if verdict not in ("yes", "no", "unsure"):
            verdict = "unsure"
        key = _pair_key(q, c, pairs)
        if key:
            out[key] = verdict
    return out


def _resolve_vector(pending: list[str], ttl_days: int,
                    result: dict[str, dict]) -> None:
    """向量路（只产出候选，不产出结论）：embedding Top-K 召回 -> 一道确认判决。"""
    try:
        ensure_entity_index()   # 惰性回填：让历史健康点位可被召回
    except Exception:
        pass
    vecs = embeddings.embed_texts(pending)
    if vecs is None:
        # 降级链第 2 档：无 embedding key / 无网络 -> 跳过向量路（关键词+词典仍有效，零回归）
        for n in pending:
            result[n] = _pack(None, "低", "none", None, None, False,
                              "embedding 不可用：跳过向量路（关键词路+别名词典仍生效）")
        return
    recalls: dict[str, list[dict]] = {}
    for n, v in zip(pending, vecs):
        try:
            hits = vector_store.search(ENTITY_COLLECTION, v, _RECALL_TOP_K)
        except Exception:
            hits = []
        nkey = knowledge.normalize_keyword(n)
        # 与查询完全同名的条目不出现在召回里（它若存在，前面 find_fresh 已处理）
        recalls[n] = [h for h in hits if h.get("key") != nkey]
    # 判决对：每名 top-_CONFIRM_PER_NAME 个候选，一次批量送判
    pairs: list[tuple[str, str]] = []
    for n in pending:
        for h in (recalls.get(n) or [])[:_CONFIRM_PER_NAME]:
            pairs.append((n, h["text"]))
    verdicts = _confirm_entities(pairs)
    for n in pending:
        hits = recalls.get(n) or []
        if not hits:
            result[n] = _pack(None, "低", "none", None, None, False, "向量库无召回")
            continue
        best = hits[0]
        verdict = verdicts.get((n, best["text"]))
        if verdict is None:
            verdict = "unsure"   # 判决未覆盖该对（LLM 漏答/降级）-> 按判不准处理
        if verdict == "yes":
            # 判决确认：写别名词典（§12.4 判决结果的缓存）-> 尝试取历史记录
            try:
                knowledge.upsert_alias(n, best["text"], "confirmed")
            except Exception:
                pass
            rec = _try_fresh(best["text"], ttl_days)
            if rec is None:
                old = _try_fresh(best["text"], _ANY_TTL_DAYS)
                note = (f"向量召回「{best['text']}」判为同一实体，但缓存已过期"
                        if old else f"向量召回「{best['text']}」判为同一实体，但无本地记录")
                result[n] = _pack(None, "高", "vector", best["text"], best["score"], True, note)
            else:
                result[n] = _pack(rec, "高", "vector", best["text"], best["score"], False,
                                  f"向量召回「{best['text']}」判决确认"
                                  f"（相似度 {best['score']}），可用历史记录")
            _log_hit(n, best["text"], best["score"], "高", "vector", True,
                     "LLM 判是：同一实体，已写别名词典")
        elif verdict == "no":
            result[n] = _pack(None, "低", "vector", best["text"], best["score"], False,
                              f"向量召回「{best['text']}」但判决为不同实体")
            _log_hit(n, best["text"], best["score"], "低", "vector", False, "LLM 判否")
        else:
            result[n] = _pack(None, "中", "vector", best["text"], best["score"], False,
                              f"疑似命中「{best['text']}」（相似度 {best['score']}），"
                              "未经确认不自动采用")
            _log_hit(n, best["text"], best["score"], "中", "vector", False,
                     "判决未确认（疑似）")


def resolve_prior_records(names: list[str], ttl_days: int) -> dict[str, dict]:
    """批量解析："这些点位之前采过吗"（混合检索 + 三级判定）。

    返回 {名字: {record, level, via, hit_name, score, expired, detail}}：
      record  仅"高 + 有新鲜记录"时非空，调用方据此直接复用（reused，不发采集）；
      level   高/中/低（§4.2 三级判定）；
      expired 高命中但缓存过期/无记录 -> 调用方进采集队列。

    调用顺序（§12.4）：find_fresh(name) -> alias_map -> find_fresh(canonical)，
    仍未命中才进入向量路（embedding Top-K 召回 + 批量判决）。"""
    result: dict[str, dict] = {}
    pending: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        n = str(raw or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        # ① 关键词路（主力：精确 + 前缀回退，沿用 find_fresh 现有逻辑）
        rec = _try_fresh(n, ttl_days)
        if rec:
            via = _keyword_via(n, rec.get("keyword") or "")
            result[n] = _pack(rec, "高", via, rec.get("keyword"), None, False,
                              f"关键词路命中「{rec.get('keyword')}」")
            _log_hit(n, rec.get("keyword"), None, "高", via, True, "关键词路命中")
            continue
        # ①-b 命中但过期（§4.2 动作表："高 + 过期" -> 进采集队列）
        old = _try_fresh(n, _ANY_TTL_DAYS)
        if old:
            via = _keyword_via(n, old.get("keyword") or "")
            result[n] = _pack(None, "高", via, old.get("keyword"), None, True,
                              f"命中历史「{old.get('keyword')}」但缓存已过期，进采集队列")
            _log_hit(n, old.get("keyword"), None, "高", via, True, "命中但过期")
            continue
        # ② 别名词典（判决结果的缓存：命中即不再调 LLM）
        canonical = None
        try:
            canonical = knowledge.lookup_alias(n)
        except Exception:
            canonical = None
        if canonical:
            rec2 = _try_fresh(canonical, ttl_days)
            if rec2:
                result[n] = _pack(rec2, "高", "alias", rec2.get("keyword"), None, False,
                                  f"别名词典命中「{canonical}」")
                _log_hit(n, canonical, None, "高", "alias", True, "词典命中")
            else:
                hit = _try_fresh(canonical, _ANY_TTL_DAYS)
                note = (f"别名词典命中「{canonical}」但缓存已过期" if hit
                        else f"别名词典命中「{canonical}」但无本地记录")
                result[n] = _pack(None, "高", "alias", canonical, None, True, note)
                _log_hit(n, canonical, None, "高", "alias", True, note)
            continue
        pending.append(n)
    # ③ 向量路兜底（只对关键词路与词典都未命中的名字）
    if pending:
        _resolve_vector(pending, ttl_days, result)
    return result


def resolve_prior_record(name: str, ttl_days: int) -> dict:
    """单名入口（批量 API 的退化情形；批量场景请用 resolve_prior_records）。"""
    out = resolve_prior_records([name], ttl_days)
    return out.get(str(name or "").strip()) or _pack(
        None, "低", "none", None, None, False, "空输入")


def register_entity(name: str, *, source: str = "crawl") -> bool:
    """把**已验证**的点位名写入 entity 向量索引（D4 写入时机的唯一入口）。

    只允许由"现场采集成功"的调用点触发（trip 验证循环 / _research）；
    "LLM 提到过就入库"是禁止的——那会把编造的点位固化成可信历史。
    无 embedding 时静默返回 False（不阻断采集主流程）。"""
    n = str(name or "").strip()
    if not n:
        return False
    vecs = embeddings.embed_texts([n])
    if not vecs:
        return False
    try:
        added = vector_store.upsert_items(ENTITY_COLLECTION, [{
            "key": knowledge.normalize_keyword(n), "text": n, "vec": vecs[0],
            "meta": {"source": source},
        }])
        return added > 0
    except Exception:
        return False


def ensure_entity_index(force: bool = False) -> int:
    """惰性回填：把 spot_cache 里"采集验证通过"（video_count>0）的点位名同步进索引。

    合规说明（D4）：这些记录本身就是采集验证的产物（与 find_fresh 认可的
    "可复用历史"同源），回填它们不违反"只在验证通过后写入"；
    LLM 提到过但未经采集的名字绝不在内。
    进程级只做一次（新写入走 register_entity 实时登记）；无 embedding 返回 0。"""
    global _BACKFILL_DONE
    if _BACKFILL_DONE and not force:
        return 0
    _BACKFILL_DONE = True
    try:
        with knowledge._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT keyword FROM spot_cache WHERE video_count > 0"
            ).fetchall()
        names = [str(r["keyword"] or "").strip() for r in rows]
        names = [n for n in dict.fromkeys(names) if n]
        if not names:
            return 0
        have = vector_store.keys(ENTITY_COLLECTION)
        todo = [n for n in names if knowledge.normalize_keyword(n) not in have]
        if not todo:
            return 0
        added = 0
        for i in range(0, len(todo), 32):
            chunk = todo[i:i + 32]
            vecs = embeddings.embed_texts(chunk)
            if not vecs:
                return added   # embedding 不可用：这里停下，已入库的保留
            added += vector_store.upsert_items(ENTITY_COLLECTION, [
                {"key": knowledge.normalize_keyword(n), "text": n, "vec": v,
                 "meta": {"source": "backfill"}}
                for n, v in zip(chunk, vecs)
            ])
        return added
    except Exception:
        return 0
