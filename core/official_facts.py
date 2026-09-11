"""官方事实三层降级（PRD §6.4 / F-C1）。

来源优先级（逐点解析，宁缺不编）：
① 仓库种子 ``data/official_facts/<城市>.yaml``——人工/社区策展，离线可断言，Demo 与黄金用例走此层；
② 高德 POI（配 Key 时）——只补"开放时间"这类一处能查到的硬事实，标 ``source=amap``，不覆盖种子；
   且**绝不把高德"人均"当门票价**（无把握即留空，交下游标"待核实"）；
③ 都没有——不返回该点，下游 ``normalize_official`` 后全空并标"待核实"，严禁填无来源数字。

只产出普通 dict（字段贴合 pipeline.decision.OfficialFact），不改任何旧签名；YAML 缺失 /
解析失败 / 高德不可用一律静默降级，绝不抛异常中断主流程（PRD §0 硬约束 6）。
"""
from __future__ import annotations

from config import PROJECT_ROOT

_SEED_DIR = PROJECT_ROOT / "data" / "official_facts"


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def load_seed_facts(city: str) -> dict[str, dict]:
    """读城市种子 YAML 的 ``spots`` 段 → {名称: 事实 dict}。

    文件缺失 / 无 spots / 解析失败一律返回 ``{}``，绝不抛。别名保留在 ``aliases`` 里，
    由 load_city_facts 做名称归并（normalize_official 会忽略未知字段，无害）。"""
    city = _clean(city)
    if not city:
        return {}
    path = _SEED_DIR / f"{city}.yaml"
    if not path.exists():
        path = _SEED_DIR / f"{city}.yml"
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    spots = data.get("spots") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    if isinstance(spots, dict):
        for name, raw in spots.items():
            key = _clean(name)
            if key and isinstance(raw, dict):
                fact = dict(raw)
                fact.setdefault("source", "seed")
                out[key] = fact
    return out


def _amap_open_hours(name: str, city: str) -> str:
    """层②：向高德取一个点的开放时间；无 Key / 失败 / 无字段一律空串。"""
    try:
        from core import geo
        detail = geo.poi_detail(name, city) or {}
        return _clean(detail.get("opentime"))
    except Exception:
        return ""


def load_city_facts(city: str, names, with_amap: bool = False) -> dict[str, dict]:
    """解析给定点名的官方事实（三层降级）。

    names：本次任务涉及的点名（档案名 + 候选名）。返回 {点名: 事实 dict}，仅包含命中
    种子或高德开放时间的点；都无则不含该点（层③=待核实）。名称匹配支持种子 aliases。"""
    seed = load_seed_facts(city)
    idx: dict[str, dict] = {}
    for nm, fact in seed.items():
        idx.setdefault(nm, fact)
        for a in (fact.get("aliases") or []):
            idx.setdefault(_clean(a), fact)

    out: dict[str, dict] = {}
    for raw in (names or []):
        nm = _clean(raw)
        if not nm or nm in out:
            continue
        fact = idx.get(nm)
        if fact is not None:
            entry = {k: v for k, v in fact.items() if k != "aliases"}
            if with_amap and not _clean(entry.get("open_hours")):
                oh = _amap_open_hours(nm, city)
                if oh:
                    entry["open_hours"] = oh
                    if not _clean(entry.get("source_url")):
                        entry["source_url"] = ""
            out[nm] = entry
        elif with_amap:
            oh = _amap_open_hours(nm, city)
            if oh:
                out[nm] = {"open_hours": oh, "source": "amap", "source_url": ""}
    return out
