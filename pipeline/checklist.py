"""出发前请自行确认清单：把报告里"会随时间变化的事实"归拢成一份待办。

设计原则（与项目既有气质一致）：
- 只搬运与归类已有事实，不生成任何新事实（宁缺不编，绝不填无来源的数字）；
- 按「要确认什么」组织，而不是按「哪个点」罗列——用户需要的是待办清单，不是名单；
- 同一信息不重复出现：一个点只在他真正需要行动的那一组里出现；
- 纯函数、零 LLM 成本、可离线断言（tests_offline.py 可直接调用）。

为什么需要它：评论区与攻略给出的票价、开放时间都是"过去时"，用户出发前必须核实。
本模块把这份"必须自己确认的事"显式列出来，而不是让报告假装永远准确——
这与质量门禁"已知妥协如实列出"是同一个诚实，只是站到了用户一侧。
"""
from __future__ import annotations

import re

# 时效敏感关键词：命中即认为该信息可能随时间变化，值得提醒出发前确认。
# 注意宁紧勿松：裸"装修"会把"装修好、服务好"这类好评误判成停业提示，故只保留强语境。
_TIME_SENSITIVE = re.compile(
    r"旺季|淡季|季节|花期|花开|雪季|演出|表演|节目|活动|节庆|市集|夜市|"
    r"临时|暂停|检修|维修|正在装修|装修停业|搬迁|停业|闭馆|闭园|限流|"
    r"预约|需提前|排队|人流|调价|涨价|降价|免费日|优惠|学生票|老年票"
)
# 高风险项（直接影响能否进得去）：排序在前，用户先看到
_HIGH_RISK = re.compile(r"预约|限流|抢票|难抢|排队|闭馆|闭园|停业|暂停|检票|检修|维修|售罄|限购")

_MAX_PER_GROUP = 6      # 单组最多展示条数，超出改为"另有 N 条"（避免信息墙）
_MAX_DETAIL = 64        # 单条说明最大长度

_GROUPS = (
    ("票价", "🎫", "报告里的票价来自网络信息，不是官方实时价；出发前请以官方渠道为准。"),
    ("开放时间与预约", "🕐", "开放时间、闭馆日与是否需要预约，出发前请到官方渠道确认。"),
    ("时效性提示", "📌", "以下条目可能随季节或档期变化，出发前请再确认一次。"),
)


def _fmt_yuan(v) -> str:
    """数值金额 → '120 元'；非数值返回空串（调用方据此跳过）。"""
    if isinstance(v, (int, float)):
        return f"{v:.0f} 元"
    return ""


def _clip(items: list[dict], limit: int = _MAX_PER_GROUP) -> tuple[list[dict], int]:
    """截断长列表：返回 (展示项, 其余条数)。"""
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _merge_same_detail(items: list[dict], threshold: int = 3, max_names: int = 8) -> list[dict]:
    """说明完全相同的条目达到阈值即合并为一条点名式（"6 个点：A、B、C…"）。

    否则会出现"票价未知"连出六行、每行只差一个点名的名单式输出——那对用户不是待办，
    是噪音。合并后同组内既保留具体信息（有价格的逐条列出），又不重复同一句话。
    """
    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for it in items:
        d = it["detail"]
        if d not in buckets:
            buckets[d] = []
            order.append(d)
        buckets[d].append(it["name"])
    out: list[dict] = []
    for d in order:
        names = buckets[d]
        if len(names) >= threshold:
            head = "、".join(names[:max_names]) + ("…" if len(names) > max_names else "")
            # _n 记真实点数：合并成一行后仍要让"共几项待办"的计数口径保持一致
            out.append({"name": f"{len(names)} 个点", "detail": f"{d} —— {head}", "_n": len(names)})
        else:
            out += [{"name": n, "detail": d, "_n": 1} for n in names]
    return out


def _selected_points(tp: dict) -> list[str]:
    """只关心真正排进行程的点——清单是出发待办，不是全量调研目录。"""
    out: list[str] = []
    seen: set[str] = set()
    for day in tp.get("itinerary") or []:
        for b in day.get("blocks") or []:
            nm = str(b.get("spot") or "").strip()
            if nm and nm not in seen:
                seen.add(nm)
                out.append(nm)
    return out


def _price_items(tp: dict, selected: list[str]) -> list[dict]:
    """票价组：有价的说"以官方为准"，无价的说"票价未知"——同一组内不重复列点。"""
    poi_cat = (tp.get("catalog") or {}).get("poi") or {}
    items: list[dict] = []
    for nm in selected:
        det = poi_cat.get(nm) or {}
        price = det.get("ticket_price")
        nature = str(det.get("ticket_nature") or "").strip()
        if not isinstance(price, (int, float)):
            items.append({"name": nm, "detail": "票价未知，需查官方渠道"})
            continue
        if float(price) == 0:
            extra = f"（{nature}）" if nature and nature != "待核实" else ""
            items.append({"name": nm, "detail": f"报告记录为免费{extra}，免费政策可能调整"})
            continue
        tail = f"，{nature}" if nature and nature != "待核实" else ""
        items.append({"name": nm, "detail": f"报告记录 {_fmt_yuan(price)}{tail}"})
    # 有确定价格的排前（具体信息优先于泛泛的"未知"）
    items.sort(key=lambda x: 1 if "未知" in x["detail"] else 0)
    return items


def _hours_items(tp: dict, selected: list[str]) -> list[dict]:
    """缺开放时间的点——最容易白跑一趟的坑。已有时间的点不再占用版面。"""
    poi_cat = (tp.get("catalog") or {}).get("poi") or {}
    items: list[dict] = []
    for nm in selected:
        det = poi_cat.get(nm) or {}
        if not str(det.get("open_hours") or "").strip():
            items.append({"name": nm, "detail": "开放时间未取到，请确认当日是否开放（含例行闭馆日）"})
    return items


def _sensitive_items(tp: dict, selected: list[str]) -> list[dict]:
    """档案的注意事项与风险里命中时效关键词的条目；高风险（预约/排队/停业）排前。"""
    snap = tp.get("snap") or {}
    profiles = snap.get("profiles") or {}
    poi_cat = (tp.get("catalog") or {}).get("poi") or {}
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for nm in selected:
        prof = profiles.get(nm) or {}
        det = poi_cat.get(nm) or {}
        texts: list[str] = []
        texts += [str(t) for t in (prof.get("tips") or [])]
        texts += [str(t) for t in (prof.get("avoid") or [])]
        for pit in det.get("pitfalls") or []:
            if isinstance(pit, dict) and pit.get("text"):
                texts.append(str(pit["text"]))
        if det.get("queue_risk"):
            texts.append(str(det["queue_risk"]))
        for t in texts:
            t = t.strip()
            if not t or not _TIME_SENSITIVE.search(t) or (nm, t) in seen:
                continue
            seen.add((nm, t))
            items.append({"name": nm, "detail": t if len(t) <= _MAX_DETAIL else t[:_MAX_DETAIL - 1] + "…",
                          "_risk": 1 if _HIGH_RISK.search(t) else 0})
    items.sort(key=lambda x: -x["_risk"])          # 稳定排序：高风险在前，其余保持档案顺序
    for it in items:
        it.pop("_risk", None)
    return _spread_by_point(items)


def _spread_by_point(items: list[dict], per_point: int = 2, target: int = 4) -> list[dict]:
    """同一景点的提示不过度集中：先每点取前 per_point 条，再按需放宽。

    实测问题：6 条时效提示里 5 条都来自同一个石窟，用户看不到其它景点的提醒。
    覆盖度优先；只有行程点很少（去重后不足 target 条）时才放宽到全量，宁多勿缺。
    """
    keep: list[dict] = []
    rest: list[dict] = []
    cnt: dict[str, int] = {}
    for it in items:
        c = cnt.get(it["name"], 0)
        if c < per_point:
            keep.append(it)
            cnt[it["name"]] = c + 1
        else:
            rest.append(it)
    return keep + rest if len(keep) < target else keep


def build_checklist(tp: dict) -> dict:
    """TripPlan(dict) → 出发前确认清单。

    返回::

        {"cutoff": "2026-09-11",
         "groups": [{"kind", "icon", "lead", "rows": [{"name", "detail"}], "more": int}],
         "footnote": "另有 N 个点暂无官方来源印证：A、B、C"}

    ``groups`` 为空表示无待确认事项，调用方据此不渲染该板块（空清单不占版面）。
    无官方来源的点收敛为一句脚注而非独立分组——避免与"票价/开放时间"两组重复罗列。
    """
    tp = tp or {}
    meta = tp.get("meta") or {}
    selected = _selected_points(tp)
    cutoff = str(meta.get("facts_cutoff") or "").strip()
    if not cutoff:
        gen = str(meta.get("generated_at") or "").strip()
        cutoff = gen[:10] if gen else ""

    groups: list[dict] = []
    for (kind, icon, lead), fn in zip(_GROUPS, (_price_items, _hours_items, _sensitive_items)):
        try:
            items = fn(tp, selected)
        except Exception:
            items = []          # 单项抽取失败不影响整块（展示层绝不中断主流程）
        if items:
            items = _merge_same_detail(items)   # 同一句话不重复 N 遍：合并为点名式
            shown, more = _clip(items)
            # 计数口径 = 涉及的点/条数（合并行按其真实点数计），否则"6 个点"会显示成 1
            total = sum(it.get("_n", 1) for it in items)
            for it in shown:
                it.pop("_n", None)
            # 键名为 rows 而非 items：Jinja2 中 `g.items` 会解析成 dict.items() 方法而不是键，
            # 模板里将永远取不到这个列表（本项目实测踩过，故从源头避开该命名）。
            groups.append({"kind": kind, "icon": icon, "lead": lead,
                           "rows": shown, "more": more, "total": total})

    verify = [str(n).strip() for n in (tp.get("to_verify") or []) if str(n).strip()]
    footnote = ""
    if verify:
        # 不再重复点名：上面的分组已列出这些点，此处只交代"它们共同缺什么"
        footnote = (f"上述 {len(verify)} 个点均暂无官方来源印证——除票价与开放时间外，"
                    "其它信息（如临时闭馆、活动档期）也建议出发前一并查证。")
    return {"cutoff": cutoff, "groups": groups, "footnote": footnote}
