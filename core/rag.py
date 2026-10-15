"""§13 文本检索（RAG）：向量库用于文本检索，不用于景点名归一化（D18）。

两个任务性质不同，两套代码，不得混用（§13.8）：
- §12 景点名归一化（core.entity_match）= **实体判定**：别名词典 + 精确/前缀匹配 + 一道确认；
- §13 文本检索（本模块）= **相似召回**：向量库 + embedding，服务两处：
  ① pipeline.ask 追问——档案切块入库，按问题检索 Top-K 片段，只把命中的片段喂给模型
     （解决"档案随行程规模增长，压缩必然丢信息；而用户的问题通常只需要其中一小部分"）；
  ② MCP 知识库检索工具（/api/kb/search）——任意 MCP 客户端按语义查本地点位知识。

切块策略（§13.5）：按"要点 / 评论条目"为最小单位，**不按固定字数切**——
语料本身就是结构化条目，硬切会破坏引用完整性（引用防幻觉依赖原文可回溯）。

降级链（§13.6，必须实现）：完整检索（embedding + vector_store）
  → 无 embedding key / 无网络：本模块全部函数返回空/None，调用方退回压缩档案路径（零回归）
  → 无 faiss：SQLite 读向量 + 纯 Python 余弦（vector_store 内部处理）
  → 全不可用：纯压缩档案。

索引写入时机：行程任务的产出本身已是"采集验证通过"的成果（§13 语料来源表），
任务完成时入库、追问时惰性回填均可；"LLM 提到过就入库"仍然禁止（D4 底线不变）。
未纳入 v1 语料：采集原始 JSON 与攻略报告 Markdown——前者需要额外提取成本、
后者缺少"要点级"无损切块方案，留待后续（不硬切、不破坏引用完整性）。
"""
from __future__ import annotations

import ast

from config import RAG_ENABLED, RAG_TOP_K
from core import embeddings, vector_store
from core.evidence import VERSION, digest_quotes, usable_points

CORPUS_COLLECTION = "corpus"
_MAX_CHUNK_CHARS = 400      # 单条片段上限（要点本身短，超出多半是异常数据）

_SOURCE_LABELS = {"trip_report": "行程档案", "city_guide": "城市攻略"}


def _clean(v) -> str:
    """字段清洗：None / "None" / "[]" 等字符串化空值一律视为空（真实数据里出现过）。"""
    s = str(v if v is not None else "").strip()
    return "" if s in ("", "None", "[]", "{}", "False", "null") else s


def _as_list(v) -> list[str]:
    """列表字段归一：真 list 直接用；字符串化的 repr（如 "['a', 'b']"）用
    ast.literal_eval 安全解析（只解析字面量，不执行代码）；其余单值包成单元素。"""
    if isinstance(v, (list, tuple)):
        return [s for s in (_clean(x) for x in v) if s]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                out = ast.literal_eval(s)
                if isinstance(out, (list, tuple)):
                    return [x for x in (_clean(i) for i in out) if x]
            except (ValueError, SyntaxError):
                pass
        return [s] if _clean(s) else []
    return []


def _result_key(result: dict) -> str:
    """语料命名空间：优先 report_name（带时间戳、天然唯一），退回 generated_at。"""
    name = _clean((result or {}).get("report_name"))
    if name:
        return f"evidence-v{VERSION}:" + name
    gen = _clean((((result or {}).get("trip_plan") or {}).get("meta") or {}).get("generated_at"))
    return f"evidence-v{VERSION}:" + (gen or "adhoc")


def chunks_from_trip_plan(tp: dict, ns: str = "") -> list[dict]:
    """把行程报告（TripPlan）切成检索片段（纯函数，独立可测）。

    覆盖 §13.4 的语料来源：提取要点 / 景点档案 / 真实评价摘要（含评论原话）/
    避坑专题 / 官方事实 / 排线理由。片段文本自带点位名（检索与引用都需要），
    meta 带 city/spot/kind/source/ns 供过滤与溯源。"""
    tp = tp or {}
    meta = tp.get("meta") or {}
    city = _clean(meta.get("city"))
    snap = tp.get("snap") or {}
    chunks: list[dict] = []
    n = 0

    def add(content: str, spot: str, kind: str, source: str = "trip_report") -> None:
        nonlocal n
        text = _clean(content)[:_MAX_CHUNK_CHARS]
        if not text:
            return
        n += 1
        label = f"{spot}·{kind}：" if spot else f"{kind}："
        chunks.append({
            "key": f"{ns}|{source}|{kind}|{spot}|{n}",
            "text": label + text,
            "meta": {"city": city, "spot": spot, "kind": kind, "source": source, "ns": ns,
                     "evidence_version": VERSION,
                     "legacy_unverified": snap.get("evidence_version") != VERSION},
        })

    # 1) 档案要点（景点与餐厅同等待遇）：亮点/避雷/美食/打卡点/贴士/花费/建议
    profiles = dict(snap.get("profiles") or {})
    foods = dict(snap.get("foods") or {})
    for spot, prof in {**profiles, **foods}.items():
        if not isinstance(prof, dict):
            continue
        for field, kind in (("highlights", "亮点"), ("avoid", "避雷"), ("food", "美食"),
                            ("photo_spots", "打卡点"), ("tips", "贴士"), ("cost_items", "花费")):
            for item in _as_list(prof.get(field)):
                add(item, str(spot).strip(), kind)
        dur = _clean(prof.get("duration_hours"))
        slot = _clean(prof.get("best_time_slot"))
        if dur or slot:
            add("建议游玩时长 " + (dur or "未标注") + "；最佳时段 " + (slot or "未标注"),
                str(spot).strip(), "建议")
    # 2) 真实评价摘要（含评论原话，引用防幻觉的原文来源）
    for spot, dg in (snap.get("digests") or {}).items():
        if not isinstance(dg, dict):
            continue
        spot = str(spot).strip()
        if _clean(dg.get("verdict")):
            add(_clean(dg.get("verdict")), spot, "真实评价")
        if _clean(dg.get("positive")):
            add("好评：" + _clean(dg.get("positive")), spot, "真实评价")
        if _clean(dg.get("negative")):
            add("差评：" + _clean(dg.get("negative")), spot, "真实评价")
        for q in digest_quotes(dg):
            add(q, spot, "评论原话")
    # 3) 避坑专题（claim + 评论原话）
    for row in snap.get("pitfall") or []:
        if not isinstance(row, dict):
            continue
        claim = _clean(row.get("claim"))
        quote = _clean(row.get("quote")) if usable_points([row]) else ""
        if not claim:
            continue
        add(claim + ("；评论原话：" + quote if quote else ""), "", "避坑")
    # 4) 官方事实（票价/开放时间等，含"待核实"状态——检索也要能召回"未确认"这件事）
    for bucket in ("poi", "food"):
        for spot, det in ((tp.get("catalog") or {}).get(bucket) or {}).items():
            if not isinstance(det, dict):
                continue
            spot = str(spot).strip()
            bits = []
            if _clean(det.get("summary")):
                bits.append(_clean(det.get("summary")))
            if det.get("ticket_price") is not None:
                try:
                    bits.append(f"票价 {float(det['ticket_price']):.0f} 元")
                except (TypeError, ValueError):
                    pass
            elif _clean(det.get("ticket_nature")):
                nature = _clean(det.get("ticket_nature"))
                # nature 也是"待核实"时不再套括号（否则输出"待核实（待核实）"，本项目曾踩过）
                bits.append("票价待核实" if nature == "待核实"
                            else f"票价待核实（{nature}）")
            for field, label in (("open_hours", "开放时间"), ("close_day", "闭馆日"),
                                 ("booking_rule", "预约规则"), ("best_slot", "推荐时段")):
                if _clean(det.get(field)):
                    bits.append(label + " " + _clean(det.get(field)))
            if bits:
                add("；".join(bits), spot, "官方信息")
    # 5) 排线理由（"为什么这样排"，追问"为什么值得去"的主要依据之一）
    for d in tp.get("itinerary") or []:
        if not isinstance(d, dict):
            continue
        for b in d.get("blocks") or []:
            if not isinstance(b, dict):
                continue
            spot = _clean(b.get("spot"))
            if spot and _clean(b.get("reasons")):
                add(_clean(b.get("reasons")), spot, "排线理由")
    return chunks


def index_result(result: dict, *, force: bool = False) -> int:
    """把一份任务结果切块入库（幂等：同命名空间已入库则跳过）。返回新增片段数。

    无 embedding / 无 trip_plan 时返回 0（静默降级）。"""
    tp = (result or {}).get("trip_plan") or {}
    if not tp:
        return 0
    ns = _result_key(result)
    if not force:
        try:
            if vector_store.has_key_prefix(CORPUS_COLLECTION, ns + "|"):
                return 0
        except Exception:
            pass
    chunks = chunks_from_trip_plan(tp, ns)
    return _index_chunks(chunks)


def index_result_complete(result: dict) -> dict:
    """独立finalizer使用：检查全部片段，补齐部分失败，不凭一个prefix宣布完成。"""
    tp = (result or {}).get("trip_plan") or {}
    chunks = chunks_from_trip_plan(tp, _result_key(result))
    missing = []
    for chunk in chunks:
        old = vector_store.get_item(CORPUS_COLLECTION, chunk["key"])
        if old is None or old.get("text") != chunk["text"] or old.get("meta") != chunk["meta"]:
            missing.append(chunk)
    written = _index_chunks(missing) if missing else 0
    complete = all((item := vector_store.get_item(CORPUS_COLLECTION, c["key"])) is not None
                   and item.get("text") == c["text"] and item.get("meta") == c["meta"] for c in chunks)
    return {"complete": complete, "written": written, "expected": len(chunks)}


def index_city_guide(city: str, guide: dict) -> int:
    """把城市攻略层（编排知识，§13.4）切块入库；重复调用时重建该城的片段。

    guide 变更（缓存过期后重采）时先删旧片段再写，避免陈旧编排建议残留。"""
    c = _clean(city)
    g = guide or {}
    if not c or (not g.get("guide_candidates") and not g.get("plan_hints")):
        return 0
    ns = f"cityguide:{c}"
    chunks: list[dict] = []
    n = 0

    def add(content: str, spot: str, kind: str) -> None:
        nonlocal n
        text = _clean(content)[:_MAX_CHUNK_CHARS]
        if not text:
            return
        n += 1
        label = f"{spot}·{kind}：" if spot else f"{kind}："
        chunks.append({
            "key": f"{ns}|city_guide|{kind}|{spot}|{n}",
            "text": label + text,
            "meta": {"city": c, "spot": spot, "kind": kind, "source": "city_guide", "ns": ns},
        })

    for m in g.get("guide_candidates") or []:
        if not isinstance(m, dict):
            continue
        name = _clean(m.get("name"))
        if not name:
            continue
        note = _clean(m.get("note"))
        add(f"（{_clean(m.get('category')) or '景点'}·攻略提及热度{_clean(m.get('heat')) or '中'}）"
            + (note or "高赞攻略视频中反复提到的点位"), name, "攻略实证")
    for h in g.get("plan_hints") or []:
        add(h, "", "编排建议")
    if _clean(g.get("stay_advice")):
        add("推荐住宿片区：" + _clean(g.get("stay_advice")), "", "住宿建议")
    if not chunks:
        return 0
    # 先确认 embedding 成功再删旧片段——否则 embedding 失败时会把老语料删光
    vecs = embeddings.embed_texts([c["text"] for c in chunks])
    if vecs is None:
        return 0
    try:
        vector_store.delete_prefix(CORPUS_COLLECTION, ns + "|")
    except Exception:
        pass
    return _index_chunks(chunks, _vecs=vecs)


def _index_chunks(chunks: list[dict], _vecs: list | None = None) -> int:
    """片段批量向量化入库（embedding 不可用时返回 0，静默降级）。

    _vecs：已算好的向量（重建路径先算后删，避免"删了写不进"）。"""
    if not chunks:
        return 0
    vecs = _vecs if _vecs is not None else embeddings.embed_texts([c["text"] for c in chunks])
    if vecs is None:
        return 0
    try:
        return vector_store.upsert_items(CORPUS_COLLECTION, [
            {"key": c["key"], "text": c["text"], "vec": v, "meta": c["meta"]}
            for c, v in zip(chunks, vecs)
        ])
    except Exception:
        return 0


def available() -> bool:
    """检索是否可能可用（不发请求）。"""
    return bool(RAG_ENABLED) and embeddings.available()


def search(query: str, *, top_k: int | None = None, prefer_ns: str = "",
           prefer_city: str = "") -> list[dict]:
    """语义检索本地点位知识。返回 [{key, text, score, meta}]，不可用时返回空列表。

    prefer_ns / prefer_city：轻加权（同报告 +0.10 / 同城市 +0.05）——问"这份报告里的
    X"时本报告片段优先，同时不排斥历史行程/攻略层里语义相关的片段（跨行程累积，§13.2）。"""
    q = _clean(query)
    if not RAG_ENABLED or not q:
        return []
    vecs = embeddings.embed_texts([q])
    if vecs is None:
        return []
    k = int(top_k or RAG_TOP_K)
    try:
        hits = vector_store.search(CORPUS_COLLECTION, vecs[0], max(k * 3, k))
    except Exception:
        return []
    # 老索引没有原文校验版本，不能继续把其中的字符串当作已验证评论原话。
    hits = [h for h in hits if (h.get("meta") or {}).get("evidence_version") == VERSION
            or (h.get("meta") or {}).get("source") == "city_guide"]
    for h in hits:
        m = h.get("meta") or {}
        bonus = 0.0
        if prefer_ns and m.get("ns") == prefer_ns:
            bonus += 0.10
        if prefer_city and m.get("city") == prefer_city:
            bonus += 0.05
        h["rank_score"] = float(h.get("score") or 0) + bonus
    hits.sort(key=lambda h: h["rank_score"], reverse=True)
    return hits[:k]


def format_hit(h: dict, ns: str = "") -> str:
    """单条命中片段的一行展示文本（含出处标注，供追问上下文与 MCP 输出复用）。"""
    m = h.get("meta") or {}
    origin = ("本报告" if ns and m.get("ns") == ns
              else (_SOURCE_LABELS.get(m.get("source") or "") or "历史档案")
              + (f" {m.get('ns')}" if m.get("ns") else ""))
    pending = "；历史内容未完成原文复核" if m.get("legacy_unverified") else ""
    return f"（{origin}{pending}）{h.get('text') or ''}"


def context_for_question(result: dict, question: str, max_chars: int = 6000) -> str | None:
    """§13 追问上下文：档案切块 -> 按问题检索 Top-K -> 只喂命中的片段 + 行程骨架。

    返回 None = 检索不可用（无 embedding / 无语料 / 非行程报告）——
    调用方必须退回现有压缩档案路径（零回归，§13.6 降级链）。"""
    tp = (result or {}).get("trip_plan") or {}
    if not tp:
        return None
    q = _clean(question)
    if not q:
        return None
    ns = _result_key(result)
    try:
        index_result(result)   # 惰性回填：历史报告也能被检索
    except Exception:
        pass
    meta = tp.get("meta") or {}
    city = _clean(meta.get("city"))
    hits = search(q, top_k=RAG_TOP_K, prefer_ns=ns, prefer_city=city)
    if not hits:
        return None
    parts = ["【报告档案（按问题语义检索命中的片段）】"]
    head = (f"城市：{city or '—'}｜天数：{meta.get('days') or '—'}｜"
            f"住宿：{_clean(meta.get('stay')) or '未指定'}")
    parts.append(head)
    skel = []
    for d in tp.get("itinerary") or []:
        if not isinstance(d, dict):
            continue
        segs = []
        for b in d.get("blocks") or []:
            if isinstance(b, dict):
                seg = f"{_clean(b.get('slot'))} {_clean(b.get('spot'))}".strip()
                if seg:
                    segs.append(seg)
        if segs:
            skel.append(f"第{d.get('day')}天：" + " → ".join(segs))
    if skel:
        parts.append("■ 行程骨架")
        parts.extend(skel)
    parts.append("■ 检索片段（与问题最相关的档案条目；标注了出处）")
    for h in hits:
        parts.append("- " + format_hit(h, ns))
    return "\n".join(parts)[:max_chars]
