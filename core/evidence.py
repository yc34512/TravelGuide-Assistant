"""绑定可追溯原文，完整检查引用，并统一管理允许下游选择的引用编号。

字面匹配只证明引用存在；结论支持性由verify中的独立批量复核处理。
内部编号不包含评论者身份。保存完整原文，避免截断掩盖编造或否定条件。
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal

VERSION = 1
MAX_SUPPORT_CONTEXT = 6000


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def registry_for(item) -> dict[str, dict]:
    """只登记实际送入提取提示词的素材，不登记第51条以后的评论。"""
    prefix = _sha(item.url)[:12]
    out = {}

    def add(suffix, kind, text, published_at):
        if not isinstance(text, str) or not text.strip():
            return
        eid = prefix + ":" + suffix + ":" + _sha(text)[:12]
        out[eid] = {"id": eid, "kind": kind, "text": text,
                    "source": item.url, "published_at": published_at or "",
                    "slot": suffix, "sha256": _sha(text)}

    add("D1", "description", item.description, item.publish_time)
    add("T1", "transcript", item.transcript, item.publish_time)
    for n, comment in enumerate(item.comments[:50], 1):
        add(f"C{n}", "comment", comment.text, comment.time)
    return out


def quote_id(evidence: dict) -> str:
    return "Q" + _sha("|".join(str(evidence.get(k, "")) for k in
                              ("id", "sha256", "start", "end")))[:20]


def bind_point(raw: dict, registry: dict[str, dict], source: str) -> dict | None:
    """模型只提议证据；编号、全文匹配与来源由代码核对，引用从原文回填。"""
    if not isinstance(raw, dict) or not source:
        return None
    supplied_source = _text(raw.get("source"))
    if supplied_source and supplied_source != source:
        return None
    eid = _text(raw.get("evidence_id"))
    excerpt = _text(raw.get("excerpt"))
    record = registry.get(eid)
    if eid:
        if record is None or not excerpt:
            return None
    else:
        # 兼容旧响应，但必须全段精确匹配；空quote不能凭空保留总结。
        excerpt = _text(raw.get("quote"))
        kind = "comment"
        if not excerpt:
            excerpt = _text(raw.get("claim")).rstrip("。")
            kind = "video"
        for candidate in registry.values():
            if (candidate["kind"] == "comment") != (kind == "comment"):
                continue
            if excerpt and excerpt in candidate["text"]:
                record = candidate
                break
        else:
            return None
    if record["source"] != source:
        return None
    start = record["text"].find(excerpt)
    if start < 0:
        return None
    evidence = dict(record, start=start, end=start + len(excerpt))
    evidence["excerpt"] = record["text"][start:evidence["end"]]
    q = dict(raw)
    q.update(source=source, evidence=evidence, evidence_id=evidence["id"],
             evidence_version=VERSION, grounding_status="matched",
             support_status="pending", support_reason="尚未完成支持性复核",
             quote=evidence["excerpt"] if evidence["kind"] == "comment" else "",
             quote_id=quote_id(evidence) if evidence["kind"] == "comment" else "")
    return q


def evidence_valid(point: dict) -> bool:
    """读入缓存时重新核对快照内容，不能仅信任旧的matched标记。"""
    e = point.get("evidence")
    if point.get("evidence_version") != VERSION or not isinstance(e, dict):
        return False
    text, excerpt = e.get("text"), e.get("excerpt")
    start, end = e.get("start"), e.get("end")
    if not isinstance(text, str) or not isinstance(excerpt, str) or not excerpt:
        return False
    if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(text)):
        return False
    if (e.get("sha256") != _sha(text) or text[start:end] != excerpt
            or e.get("source") != point.get("source")
            or e.get("id") != point.get("evidence_id")
            or e.get("kind") not in {"comment", "description", "transcript"}):
        return False
    expected = _sha(str(point["source"]))[:12] + ":"
    if e.get("id") != expected + str(e.get("slot") or "") + ":" + e["sha256"][:12]:
        return False
    if e["kind"] == "comment":
        return point.get("quote") == excerpt and point.get("quote_id") == quote_id(e)
    return not point.get("quote") and not point.get("quote_id")


def usable_points(points: list[dict]) -> list[dict]:
    """只有原文匹配且通过支持性复核的结论才能进入规划与确定事实。"""
    return [p for p in points if isinstance(p, dict)
            and evidence_valid(p) and p.get("support_status") == "supported"]


def support_rule_reason(point: dict) -> str:
    """只做明确的数值/年龄条件检查；通过这些规则不等于语义支持。"""
    e = point.get("evidence") or {}
    context = e.get("text") or ""
    if len(context) > MAX_SUPPORT_CONTEXT:
        return "证据上下文过长，未完成支持性复核"
    claim = str(point.get("claim") or "")
    if len(claim) > 600:
        return "结论过长，未完成支持性复核"
    numbers = lambda text: {Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", text)}
    if not numbers(claim).issubset(numbers(context)):
        return "结论包含原文没有的数字"
    age = re.search(r"(\d+)\s*岁\s*(以上|以下|及以上|及以下)", context)
    if (age and re.search(r"老人|老年", claim) and re.search(r"免票|免费", claim)
            and (age[1] not in claim or age[2] not in claim)):
        return "结论遗漏原文的年龄限制"
    return ""


def quote_bank(points: list[dict]) -> dict[str, dict]:
    """引用名单来自已复核要点，保留整段原文、来源与证据快照。"""
    out = {}
    for p in usable_points(points):
        if p.get("quote") and p.get("quote_id"):
            out[p["quote_id"]] = {"id": p["quote_id"], "text": p["quote"],
                                  "source": p["source"], "point": p}
    return out


def select_quotes(ids, bank: dict[str, dict], limit: int = 2) -> list[dict]:
    """模型只能选择名单中的ID；代码去重并回填，拒绝陌生编号。"""
    if not isinstance(ids, list):
        return []
    selected = []
    seen = set()
    for qid in ids:
        if not isinstance(qid, str) or qid in seen or qid not in bank:
            continue
        record = bank[qid]
        point = record.get("point") if isinstance(record, dict) else None
        if (not isinstance(point, dict) or not usable_points([point])
                or record.get("text") != point.get("quote")
                or record.get("source") != point.get("source")
                or qid != point.get("quote_id")):
            continue
        seen.add(qid)
        selected.append(record)
        if len(selected) >= limit:
            break
    return selected


def digest_quotes(digest: dict) -> list[str]:
    """历史摘要缺少证据时不再当作已验证评论原话；新摘要按证据回填。"""
    points = digest.get("quote_points") or []
    if not isinstance(points, list):
        return []
    bank = quote_bank(points)
    return [r["text"] for r in select_quotes(digest.get("quote_ids"), bank)]
