"""复杂追问的受控工具：schema、参数校验、权限与执行。

首版四个工具只读/查询：
- read_report：本人报告的结构化行程、约束、质量与证据清单；
- search_knowledge：本人报告片段或公共城市攻略；服务端强制过滤归属；
- get_evidence：按证据 ID 返回确切原文、来源与资料时间；
- query_route：已验证 POI ID 之间的地图路线，计入调用预算。

模型不能传任意 URL/SQL/路径/owner，也不能执行 shell 或任意 HTTP。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from config import (AGENT_MAX_CONTEXT_CHARS, AGENT_MAX_EMBED_ATTEMPTS,
                    AGENT_MAX_ROUTE_CALLS, AGENT_MAX_TOOL_CALLS)

_MAX_EVIDENCE = 12
_MAX_SEARCH_K = 8
_MAX_TEXT = 700


def _truncate(text: str, limit: int = _MAX_TEXT) -> tuple[str, bool]:
    s = str(text or "")
    if len(s) <= limit:
        return s, False
    return s[:limit - 1] + "…", True


def unified(*, ok: bool, data=None, evidence_refs: list[str] | None = None,
            as_of: str | None = None, source_version: str | None = None,
            error_code: str | None = None, retryable: bool = False,
            truncated: bool = False) -> dict:
    return {"ok": bool(ok), "data": data, "evidence_refs": evidence_refs or [],
            "as_of": as_of, "source_version": source_version,
            "error_code": error_code, "retryable": bool(retryable),
            "truncated": bool(truncated)}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_report",
            "description": "读取本人这份报告的行程、每日安排、约束、质量问题和可用证据编号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_id": {"type": "string", "description": "留空则读当前报告"},
                    "version_id": {"type": "string", "description": "留空则读当前版本"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": ("按语义检索本人报告片段或公共城市攻略。scope=report 只搜当前报告；"
                            "scope=city 只搜公共城市攻略。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string", "enum": ["report", "city", "auto"]},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": _MAX_SEARCH_K},
                },
                "required": ["query", "scope"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_evidence",
            "description": "按证据编号返回确切原文、来源与资料时间；不得自行编造引文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "evidence_ids": {"type": "array", "items": {"type": "string"},
                                     "maxItems": _MAX_EVIDENCE},
                },
                "required": ["evidence_ids"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_route",
            "description": ("查询报告内两个 POI（必须使用 read_report 返回的 poi_id）之间的"
                            "路线估计；只允许受控地图调用或有效缓存。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_poi_id": {"type": "string"},
                    "destination_poi_id": {"type": "string"},
                    "mode": {"type": "string", "enum": ["transit", "walking", "driving", "mixed"]},
                    "departure_time": {"type": "string",
                                       "description": "ISO 时间或留空；服务商为当前估计"},
                },
                "required": ["origin_poi_id", "destination_poi_id", "mode"],
                "additionalProperties": False,
            },
        },
    },
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


@dataclass
class ToolBudget:
    max_tool_calls: int = AGENT_MAX_TOOL_CALLS
    max_route_calls: int = AGENT_MAX_ROUTE_CALLS
    max_embed_attempts: int = AGENT_MAX_EMBED_ATTEMPTS
    tool_calls: int = 0
    route_calls: int = 0
    embed_attempts: int = 0
    stop_reason: str = ""

    def take_tool(self) -> bool:
        if self.tool_calls >= self.max_tool_calls:
            self.stop_reason = "tool_budget_exhausted"
            return False
        self.tool_calls += 1
        return True

    def take_route(self) -> bool:
        if self.route_calls >= self.max_route_calls:
            self.stop_reason = "route_budget_exhausted"
            return False
        self.route_calls += 1
        return True

    def take_embed(self) -> bool:
        if self.embed_attempts >= self.max_embed_attempts:
            self.stop_reason = "embedding_budget_exhausted"
            return False
        self.embed_attempts += 1
        return True


def poi_id(city: str, name: str) -> str:
    return "poi_" + hashlib.sha256(
        f"{city or ''}|{name or ''}".encode("utf-8")).hexdigest()[:12]


def build_poi_index(tp: dict) -> dict[str, dict]:
    city = str((tp.get("meta") or {}).get("city") or "")
    out: dict[str, dict] = {}
    for day in tp.get("itinerary") or []:
        for block in day.get("blocks") or []:
            name = str(block.get("spot") or "").strip()
            if name:
                out.setdefault(poi_id(city, name), {"id": poi_id(city, name), "name": name})
    for bucket in ("poi", "food"):
        for name in ((tp.get("catalog") or {}).get(bucket) or {}):
            n = str(name or "").strip()
            if n:
                out.setdefault(poi_id(city, n), {"id": poi_id(city, n), "name": n})
    return out


@dataclass
class ToolContext:
    owner_id: str
    report: dict
    store: object | None = None
    job_id: str | None = None
    budget: ToolBudget = field(default_factory=ToolBudget)
    route_fn: Callable[[str, str, str, str], dict] | None = None

    @property
    def result(self) -> dict:
        return self.report.get("result") or {}

    @property
    def trip_plan(self) -> dict:
        return self.result.get("trip_plan") or {}

    @property
    def report_id(self) -> str:
        return self.report.get("report_id") or ""

    @property
    def version_id(self) -> str:
        return self.report.get("version_id") or ""

    @property
    def city(self) -> str:
        return str((self.trip_plan.get("meta") or {}).get("city") or "")

    @property
    def as_of(self) -> str:
        meta = self.trip_plan.get("meta") or {}
        return str(meta.get("facts_cutoff") or meta.get("generated_at") or "")

    def evidence_map(self) -> dict[str, dict]:
        return {str(e.get("evidence_id")): e for e in (self.report.get("evidence") or [])
                if e.get("evidence_id")}


def _geo_available() -> bool:
    try:
        from core import geo
        return bool(geo.available())
    except Exception:
        return False


def _read_report(args: dict, ctx: ToolContext) -> dict:
    rid = str(args.get("report_id") or ctx.report_id)
    vid = str(args.get("version_id") or ctx.version_id)
    if rid != ctx.report_id or vid != ctx.version_id:
        return unified(ok=False, error_code="not_found",
                       source_version=ctx.version_id, as_of=ctx.as_of)
    tp = ctx.trip_plan
    itinerary = []
    truncated = False
    for day in (tp.get("itinerary") or [])[:7]:
        blocks = []
        for b in (day.get("blocks") or [])[:10]:
            if not isinstance(b, dict):
                continue
            name = str(b.get("spot") or "").strip()
            blocks.append({"slot": b.get("slot"), "spot": name,
                           "poi_id": poi_id(ctx.city, name) if name else "",
                           "duration": b.get("duration")})
        itinerary.append({"day": day.get("day"), "blocks": blocks})
        if len(day.get("blocks") or []) > 10:
            truncated = True
    evidence = list(ctx.evidence_map().values())
    if len(evidence) > 50:
        evidence = evidence[:50]
        truncated = True
    quality = tp.get("quality") or {}
    data = {
        "report_id": ctx.report_id, "version_id": ctx.version_id,
        "meta": tp.get("meta") or {}, "itinerary": itinerary,
        "constraints": (quality.get("constraint_coverage") or {}),
        "quality": {"score": quality.get("score"), "passed": quality.get("passed"),
                    "unresolved": (quality.get("unresolved") or [])[:20]},
        "evidence_ids": [e["evidence_id"] for e in evidence],
        "pois": list(build_poi_index(tp).values())[:40],
        "available": {"route": bool(ctx.route_fn) or _geo_available()},
    }
    return unified(ok=True, data=data,
                   evidence_refs=[e["evidence_id"] for e in evidence[:10]],
                   as_of=ctx.as_of, source_version=ctx.version_id, truncated=truncated)


def _search_knowledge(args: dict, ctx: ToolContext) -> dict:
    q = str(args.get("query") or "").strip()
    scope = str(args.get("scope") or "auto")
    try:
        k = max(1, min(_MAX_SEARCH_K, int(args.get("top_k") or 5)))
    except (TypeError, ValueError):
        k = 5
    if not q:
        return unified(ok=False, error_code="invalid_argument", retryable=False)
    if scope not in ("report", "city", "auto"):
        return unified(ok=False, error_code="invalid_argument", retryable=False)
    from core import rag
    if not rag.available():
        return unified(ok=False, error_code="retrieval_unavailable", retryable=False,
                       source_version=ctx.version_id, as_of=ctx.as_of)
    if not ctx.budget.take_embed():
        return unified(ok=False, error_code="embedding_budget_exhausted",
                       retryable=False, source_version=ctx.version_id, as_of=ctx.as_of)
    ns = f"evidence-v1:{ctx.result.get('report_name') or ''}"
    hits = rag.search(q, top_k=k, prefer_ns=ns, prefer_city=ctx.city)
    out = []
    for h in hits:
        m = h.get("meta") or {}
        if scope == "report" and m.get("ns") != ns:
            continue
        if scope == "city" and m.get("source") != "city_guide":
            continue
        if scope == "auto" and not (m.get("ns") == ns or m.get("source") == "city_guide"):
            continue
        text, cut = _truncate(h.get("text") or "")
        out.append({"text": text, "score": h.get("score"), "spot": m.get("spot") or "",
                    "kind": m.get("kind") or "", "source": m.get("source") or "",
                    "ns": m.get("ns") or "", "truncated": cut})
    return unified(ok=bool(out), data=out[:k], evidence_refs=[],
                   as_of=ctx.as_of, source_version=ctx.version_id,
                   error_code=None if out else "no_relevant_knowledge",
                   retryable=False)


def _get_evidence(args: dict, ctx: ToolContext) -> dict:
    ids = args.get("evidence_ids")
    if not isinstance(ids, list) or not ids:
        return unified(ok=False, error_code="invalid_argument", retryable=False)
    ids = [str(x) for x in ids[:_MAX_EVIDENCE]]
    known = ctx.evidence_map()
    rows, missing, truncated = [], [], False
    for eid in ids:
        ev = known.get(eid)
        if ev is None:
            missing.append(eid)
            continue
        quote, cut = _truncate(ev.get("quote") or "")
        truncated = truncated or cut
        rows.append({"evidence_id": eid, "quote": quote, "source": ev.get("source") or "",
                     "spot": ev.get("spot") or "", "kind": ev.get("kind") or "",
                     "as_of": ev.get("as_of") or ctx.as_of, "truncated": cut})
        if len(ev.get("quote") or "") > _MAX_TEXT + 100:
            truncated = True
    return unified(ok=bool(rows), data={"evidence": rows, "missing": missing},
                   evidence_refs=[r["evidence_id"] for r in rows],
                   as_of=ctx.as_of, source_version=ctx.version_id,
                   error_code=None if rows else "evidence_not_found",
                   retryable=False, truncated=truncated)


def _query_route(args: dict, ctx: ToolContext) -> dict:
    origin = str(args.get("origin_poi_id") or "")
    dest = str(args.get("destination_poi_id") or "")
    mode = str(args.get("mode") or "mixed")
    departure = str(args.get("departure_time") or "")
    if mode not in ("transit", "walking", "driving", "mixed"):
        return unified(ok=False, error_code="invalid_argument", retryable=False)
    pois = build_poi_index(ctx.trip_plan)
    if origin not in pois or dest not in pois or origin == dest:
        return unified(ok=False, error_code="invalid_poi_id", retryable=False,
                       source_version=ctx.version_id, as_of=ctx.as_of)
    if not ctx.budget.take_route():
        return unified(ok=False, error_code="route_budget_exhausted", retryable=False,
                       source_version=ctx.version_id, as_of=ctx.as_of)
    if ctx.route_fn is not None:
        result = ctx.route_fn(pois[origin]["name"], pois[dest]["name"], mode, departure)
        return unified(ok=bool(result.get("advice")), data=result,
                       as_of=result.get("as_of") or ctx.as_of,
                       source_version=ctx.version_id,
                       error_code=None if result.get("advice") else "route_unavailable",
                       retryable=bool(result.get("retryable", False)))
    from core import geo
    if not geo.available():
        return unified(ok=False, error_code="route_unavailable", retryable=False,
                       source_version=ctx.version_id, as_of=ctx.as_of)
    try:
        a = geo.geocode_poi(pois[origin]["name"], ctx.city)
        b = geo.geocode_poi(pois[dest]["name"], ctx.city)
        if not a or not b:
            return unified(ok=False, error_code="route_unavailable", retryable=False,
                           source_version=ctx.version_id, as_of=ctx.as_of)
        meta = geo.route_advice_meta(a["location"], b["location"], ctx.city)
        data = {"advice": meta.get("advice"), "cache_hit": meta.get("cache_hit"),
                "mode": mode, "provider_mode": "mixed", "departure_time": departure,
                "as_of": meta.get("as_of"), "origin": pois[origin]["name"],
                "destination": pois[dest]["name"], "source": "amap"}
        return unified(ok=bool(data["advice"]), data=data,
                       as_of=ctx.as_of, source_version=ctx.version_id,
                       error_code=None if data["advice"] else "route_unavailable",
                       retryable=False)
    except Exception:
        return unified(ok=False, error_code="route_error", retryable=True,
                       source_version=ctx.version_id, as_of=ctx.as_of)


_EXECUTORS = {
    "read_report": _read_report,
    "search_knowledge": _search_knowledge,
    "get_evidence": _get_evidence,
    "query_route": _query_route,
}


def execute(name: str, args: dict, ctx: ToolContext) -> dict:
    """校验名称/参数/预算后执行；异常不外泄成任意操作。"""
    if name not in TOOL_NAMES:
        return unified(ok=False, error_code="unknown_tool", retryable=False)
    if not isinstance(args, dict):
        return unified(ok=False, error_code="invalid_argument", retryable=False)
    if not ctx.budget.take_tool():
        return unified(ok=False, error_code="tool_budget_exhausted", retryable=False,
                       source_version=ctx.version_id, as_of=ctx.as_of)
    try:
        return _EXECUTORS[name](args, ctx)
    except Exception as exc:
        return unified(ok=False, error_code="tool_error", retryable=True,
                       data={"error": type(exc).__name__},
                       source_version=ctx.version_id, as_of=ctx.as_of)


def compact_result(result: dict, limit: int = 5000) -> str:
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= limit:
        return raw
    data_text = json.dumps(result.get("data"), ensure_ascii=False)
    data_text, _ = _truncate(data_text, max(300, limit - 500))
    return json.dumps({**result, "data": data_text, "truncated": True},
                      ensure_ascii=False)