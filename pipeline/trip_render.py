"""TripPlan 只读双渲染（PRD F-G1 / F-G2 / §12，M4b-2）。

表达层铁律：本模块只读 pipeline.decision.build_trip_plan 产出的统一对象，
不做任何计算、补数或改结论；概览/避坑/交通等渲染输入由编排层预先整理进 plan["snap"]。
- render_markdown：0~10 报告信息架构（§12.1），榜单→详情库锚点下钻（§11.4）。
- render_html：把 TripPlan 投影成既有 Jinja2 模板 context（零计算），复用模板资产。

报告不输出任何金额估算（预算模块已退役，只保留调研到的确定门票价这一事实信息）。
防御性：任一区块缺数据给「最小可用 + 待核实」版本，整块降级跳过，绝不崩溃、绝不编造。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.checklist import build_checklist   # 待确认清单：只归类已有事实，不产生新结论
from pipeline.planner import normalize_notes   # notes 字符串归一化（planner 不反向依赖，无环）

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# 呈现常量（纯展示，不含结论）
_NOTE_ICONS = {"避坑": "❌", "费用": "💰", "时间": "🕐", "提示": "💡"}
_STATE_ICONS = {"入选": "✅", "备选": "🔁", "淘汰": "❌"}
_QC_ICONS = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}

DISCLAIMER = (
    "> 本行程由 AI 基于抖音公开内容与地图数据生成，仅供参考；\n"
    "> 开放时间、票价、班次等时效信息出发前请务必通过官方渠道核实。")


def _cell(v) -> str:
    """MD 表格单元格转义（| 会破坏表格）。"""
    return str(v if v is not None else "").replace("|", "\\|") or "—"


def _money(v) -> str:
    return f"{v:.0f} 元" if isinstance(v, (int, float)) else "待核实"


def _ticket_line(detail: dict) -> str:
    """门票同源行：catalog 权威价 + 性质（R12 的「同一事实」在 MD 的唯一出口）。"""
    price = detail.get("ticket_price")
    nature = detail.get("ticket_nature") or "待核实"
    src = f"，来源：{detail['ticket_source']}" if detail.get("ticket_source") else ""
    if price is None:
        # nature 也兜底成"待核实"时不再套括号：曾输出"门票：待核实（待核实）"（一份报告 15 处）
        return f"- 门票：待核实{src}" if nature == "待核实" else f"- 门票：待核实（{nature}）{src}"
    return f"- 门票：{price:.0f} 元（{nature}）{src}"


def slot_price_labels(block: dict) -> tuple[str, str]:
    """行程槽位的门票文案（MD 行 / HTML 标签），只读详情卡同源票价（catalog.ticket_price）。

    只展示调研到的确定门票价（同源事实）；无票价来源时返回空串——报告不输出任何金额
    估算，预算由用户自行考虑。返回 (markdown 行文案, html 标签文案)。纯函数可测。"""
    tp = block.get("ticket_price")
    if isinstance(tp, (int, float)):
        free = float(tp) == 0
        return (("门票：免费（同源详情）" if free else f"门票：{tp:.0f} 元（同源详情）"),
                "门票 免费" if free else f"门票 {tp:.0f} 元")
    return ("", "")


# —— 详情卡空壳判定 ——
# catalog 里含未调研/采集失败的点（det 为空字典），若不判定就无条件输出
# "门票：待核实 / 建议时长：见要点"，详情库会堆出大量空壳卡（实测一份报告 11 个），
# 榜单 [详情] 点进去什么都没有。锚点仍保留，否则热度榜链接会变成 R11 死链。
_POI_CONTENT_KEYS = ("summary", "highlights", "photo_spots", "pitfalls", "open_hours",
                     "close_day", "booking_rule", "sources", "duration_hours", "best_slot",
                     "guide_note")
_FOOD_CONTENT_KEYS = ("avg_price", "signature_dishes", "pitfalls", "queue_risk",
                      "nearest_poi", "sources", "guide_note")
EMPTY_CARD_NOTE = "- 暂无档案数据：未进入调研或采集未获内容（原因见选点决策表）"


def _has_poi_content(det: dict, extra_p: dict, hrow: dict | None, digest) -> bool:
    """景点详情卡是否有实质内容（纯函数可测）。"""
    det = det or {}
    if any(det.get(k) for k in _POI_CONTENT_KEYS) or det.get("ticket_price") is not None:
        return True
    if any((extra_p or {}).get(k) for k in ("tips", "food")):
        return True
    return bool((hrow or {}).get("videos")) or bool(digest)


def _has_food_content(det: dict, extra_f: dict, digest) -> bool:
    """美食详情卡是否有实质内容（纯函数可测）。"""
    det = det or {}
    if any(det.get(k) for k in _FOOD_CONTENT_KEYS):
        return True
    return bool((extra_f or {}).get("tips")) or bool(digest)


def _digest_lines(dg: dict) -> list[str]:
    out: list[str] = []
    if isinstance(dg, dict) and dg.get("verdict"):
        out.append(f"- 真实评价摘要：{dg['verdict']}")
        if dg.get("positive"):
            out.append(f"  - 好评：{dg['positive']}")
        if dg.get("negative"):
            out.append(f"  - 差评：{dg['negative']}")
        for q in dg.get("quotes") or []:
            out.append(f"  > 评论摘录：\"{q}\"")
    return out


def _conf_badge(row: dict) -> str:
    level = row.get("conf_level")
    if not level:
        return row.get("confidence", "单源")
    n = row.get("n_sources") or 1
    return f"{level}·{n}来源" if n > 1 else level


# —— Markdown：0~11 信息架构，只读 TripPlan ——

def render_markdown(plan: dict) -> str:
    meta = plan.get("meta") or {}
    snap = plan.get("snap") or {}
    quality = plan.get("quality") or {}
    city = meta.get("city") or "目的地"
    days = meta.get("days") or 0
    lines: list[str] = [f"# 《{city}》{days} 天行程规划", ""]

    # 0 需求回显条 + 行程概览卡
    echo = [f"目的地 **{city}**", f"天数 **{days} 天**"]
    if meta.get("stay"):
        echo.append(f"住宿 **{meta['stay']}**")
    if meta.get("preference_mode"):
        echo.append(f"消费偏好 **{meta['preference_mode']}**")
    if meta.get("prefs"):
        echo.append(f"特别偏好 **{meta['prefs']}**")
    if snap.get("user_spots"):
        echo.append(f"指定景点 **{'、'.join(snap['user_spots'])}**")
    gen = (str(meta.get("generated_at") or "")[:16].replace("T", " ")
           or datetime.now().strftime("%Y-%m-%d %H:%M"))
    n_spots = len((plan.get("catalog") or {}).get("poi") or {})
    n_foods = len((plan.get("catalog") or {}).get("food") or {})
    lines += [f"> 你的需求：{' ｜ '.join(echo)}",
              "> 如与预期不符，可在网页调整参数后重新生成",
              f"> 生成时间：{gen}",
              f"> 数据来源：{n_spots} 个景点" + (f" + {n_foods} 家餐厅" if n_foods else "")
              + "的抖音实地调研",
              f"> 路线依据：{'高德地图实测（公交线路/耗时）' if snap.get('geo_on') else 'LLM 交通估算（未配置高德 Key，线路与耗时均为估算，出发前以地图 App 为准）'}",
              f"> 采集档位：{meta.get('collect_mode') or '—'}"
              f"（事实核验截至 {meta.get('facts_cutoff') or '—'}）",
              ""]
    ov = snap.get("overview") or {}
    lines += ["## 行程概览", ""]
    if ov:
        spot_line = (f"- 总天数 **{ov.get('days', days)} 天** ｜ 景点 **{ov.get('spots', 0)} 个**"
                     f" ｜ 行程点 **{ov.get('slots', 0)} 个**")
        if ov.get("foods"):
            spot_line += f" ｜ 美食候选 **{ov['foods']} 家**"
        lines.append(spot_line)
        lines += [f"- 亮点 **{ov.get('highlights', 0)} 条** ｜ 避坑提示 **{ov.get('pitfalls', 0)} 条**"]
    if snap.get("guide_note"):
        # 数据来源透明化（M6-B）：圈定与排线的实证依据到底从哪来，用户看得见
        lines.append(f"- 📚 {snap['guide_note']}")
    if snap.get("summary_note"):
        lines.append(f"- 规划说明：{snap['summary_note']}")
    if snap.get("dedupe_note"):
        lines.append(f"- ℹ️ {snap['dedupe_note']}")
    if snap.get("rebalance_note"):
        lines.append(f"- 🔀 {snap['rebalance_note']}")
    lines.append("")

    # 1 质量分卡
    if quality:
        lines += ["## 质量分卡", "",
                  f"- 质量总分 **{quality.get('score', 0)}** / 100 ｜ 回炉 **{quality.get('repair_rounds', 0)}** 轮"
                  f" ｜ 已知妥协 **{len(quality.get('unresolved') or [])}** 项", ""]
        for c in quality.get("checks") or []:
            icon = _QC_ICONS.get(c.get("status"), "")
            note = f" — {c['note']}" if c.get("note") else ""
            lines.append(f"- {icon} **{c.get('rule_id')} {c.get('name')}**：{c.get('status')}"
                         f"（{c.get('actual', '')}）{note}")
        lines.append("")
        if quality.get("unresolved"):
            lines += ["### 已知妥协 / 待你定夺", "",
                      "> 以下是回炉后仍未能完全满足的项，如实列出而非静默掩盖：", ""]
            lines += [f"- {u}" for u in quality["unresolved"]]
            lines.append("")

    # 2 攻略介绍（What）
    intro = plan.get("intro") or []
    if intro:
        lines += ["## 攻略介绍", ""]
        for sec in intro:
            lines += [f"### {sec.get('title')}", "", sec.get("body") or "（待补充）", ""]
            if sec.get("facts_ref"):
                lines.append(f"<sub>依据：{'、'.join(sec['facts_ref'])}（同源证据，未另造数字）</sub>")
                lines.append("")

    # 3 选点决策表
    rows = plan.get("decision_table") or []
    if rows:
        lines += ["## 选点决策表", "",
                  "> 每个被圈定的候选都有明确结论——包括没带你去的点及原因。", "",
                  "| 状态 | 名称 | 类别 | 来源 | 证据 | 热度（趋势） | 营销号 | 理由 |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            state = r.get("state") or "未定"
            heat = f"{r['heat_score']:.2f}" if r.get("heat_score") else "—"
            mkt = f"{r['mkt_ratio']:.0%}" if r.get("mkt_ratio") else "—"
            lines.append(f"| {_STATE_ICONS.get(state, '❔')} {state} | {_cell(r.get('name'))} "
                         f"| {_cell(r.get('category'))} | {_cell(r.get('sources'))} | {_cell(r.get('evidence'))} "
                         f"| {heat}（{_cell(r.get('heat_trend'))}） | {mkt} | {_cell(r.get('reason'))} |")
        lines.append("")

    # 4 逐日行程（How）+ 分段交通
    poi_cat = (plan.get("catalog") or {}).get("poi") or {}
    if plan.get("itinerary") and poi_cat:
        lines += ["> 提示：点击景点名会弹出详情卡（介绍 / 避雷 / 贴士与来源），关闭即可回到原位。", ""]
    for d in plan.get("itinerary") or []:
        lines += [f"## 第 {d.get('day')} 天", ""]
        for s in d.get("blocks") or []:
            spot = str(s.get("spot") or "")
            # 行程点做成锚点链接：行程 ↔ 详情互相直达（避雷/介绍在详情卡里）
            head = f"[{spot}](#spot-{spot})" if spot in poi_cat else spot
            lines.append(f"### {s.get('slot')} · {head}")
            if s.get("duration"):
                lines.append(f"- 游玩时长：{s['duration']}")
            price_md, _ = slot_price_labels(s)
            if price_md:
                lines.append(f"- {price_md}")
            if s.get("open_hours"):
                lines.append(f"- 开放时间：{s['open_hours']}（同源详情）")
            if s.get("transport"):
                lines.append(f"- 交通：{s['transport']}")
            if s.get("reasons"):
                lines.append(f"- 值得去：{s['reasons']}")
            for nt in (normalize_notes(s["notes"]) if isinstance(s.get("notes"), str)
                       else (s.get("notes") or [])):
                mark = "**" if nt.get("type") in ("避坑", "费用", "时间") else ""
                lines.append(f"- {_NOTE_ICONS.get(nt.get('type'), '💡')} {mark}{nt.get('type')}：{nt.get('text')}{mark}")
            for q in s.get("pitfall_quotes") or []:
                lines.append(f"  > 避坑引用：\"{q}\"")
            if s.get("food"):
                lines.append(f"- 美食：{s['food']}（就近自选，见美食榜）")
            lines.append("")
    legs = snap.get("legs") or []
    if legs:
        lines += ["## 分段交通", ""]
        for lg in legs:
            mn = f"约{lg['minutes']}分钟" if lg.get("minutes") else "时长未知"
            tag = "实测" if lg.get("nature") == "实测" else "估算"
            extra = f"｜{lg['note']}" if lg.get("note") else ""
            lines.append(f"- 第{lg.get('day', '?')}日 {lg.get('from')}→{lg.get('to')}："
                         f"{lg.get('mode', '')}·{mn}（{tag}）{extra}")
        lines += ["", "> 交通时长为市内点对点估算，实际以地图 App 为准。", ""]

    # 5 避坑专题
    pitfall = snap.get("pitfall") or []
    if pitfall:
        lines += ["## 避坑专题（附评论原文 · 高置信度排前）", ""]
        for i, row in enumerate(pitfall, 1):
            lines.append(f"{i}. **{row.get('claim')}**（{_conf_badge(row)}）")
            if row.get("quote"):
                lines.append(f"   > 评论原文：\"{row['quote']}\"")
            if row.get("source"):
                lines.append(f"   > 来源：{row['source']}")
        lines.append("")

    # 6 Plan B（排队/天气等预期之外的情况怎么换）
    plan_b = plan.get("plan_b") or []
    if plan_b:
        lines += ["## Plan B（预期之外时怎么换）", ""]
        for p in plan_b:
            lines.append(f"- {p}")
        lines.append("")

    # 6b 出发前请自行确认：把"会随时间变化的事实"归拢成待办，而不是罗列名单。
    # 报告只承诺"给个大概"，那"哪些必须你自己核实"就该显式写清楚——
    # 这与质量门禁「已知妥协如实列出」是同一种诚实，只是站到了用户一侧。
    checklist = build_checklist(plan)
    if checklist["groups"] or checklist["footnote"]:
        lines += ["## 出发前请自行确认", ""]
        if checklist["cutoff"]:
            lines += [f"> 本报告信息截至 {checklist['cutoff']}；以下事项来自网络公开信息，"
                      "会随时间变化，出发前请以官方渠道为准。", ""]
        for g in checklist["groups"]:
            lines.append(f"**{g['icon']} {g['kind']}**")
            lines.append(f"> {g['lead']}")
            lines.append("")
            for it in g["rows"]:
                lines.append(f"- **{it['name']}**：{it['detail']}")
            if g.get("more"):
                lines.append(f"- （另有 {g['more']} 条同类提示，详见下方详情库）")
            lines.append("")
        if checklist["footnote"]:
            lines += [f"> {checklist['footnote']}", ""]

    # 7 热度榜（可下钻）
    heat_ranking = plan.get("heat_ranking") or []
    if heat_ranking:
        lines += ["## 热度榜（近 90 天抖音数据）", "",
                  "> 热度指数 = 点赞热度 40%（对数归一）+ 评论密度 30% + 新鲜度 30%；",
                  "> 营销号占比 ≥50% 需谨慎看待；点「详情」跳转详情库对应档案。", ""]
        for r in heat_ranking:
            extra = f" ｜ ⚠️ 未入选：{r['not_selected_reason']}" if r.get("not_selected_reason") else ""
            tags = f" ｜ {'、'.join(r['tags'])}" if r.get("tags") else ""
            lines.append(f"{r.get('rank', 0)}. **{r.get('ref_id')}** ｜ 热度 {r.get('score', 0):.2f}"
                         f" ｜ {r.get('trend') or '—'} ｜ 状态 {r.get('state') or '—'}{tags}"
                         f" ｜ [详情](#spot-{r.get('ref_id')}){extra}")
        lines.append("")

    # 8 美食榜（可下钻；F-F2 独立口径：多维可复算推荐分，不蹭景点热度）
    food_ranking = plan.get("food_ranking") or []
    if food_ranking:
        lines += ["## 美食榜（未排入行程时间线，按就近/人均自选）", ""]
        if snap.get("food_sample_note"):
            lines += [f"> ⚠️ {snap['food_sample_note']}", ""]
        lines += ["> 排序口径：好评证据 35% + 人均信息完备 25% + 热度 15% + 口碑标签 15% + 排队风险 10%（可复算）；",
                  "> 正餐与小吃分列样本，人均带样本数，不被单个极值拉低。", ""]
        food_cat = (plan.get("catalog") or {}).get("food") or {}
        for r in food_ranking:
            det = food_cat.get(r.get("ref_id")) or {}
            avg = f"人均 {det['avg_price']:.0f} 元" if det.get("avg_price") is not None else "人均待核实"
            n = f"（{det['price_samples_n']} 样本）" if det.get("price_samples_n") else ""
            q = f" ｜ 排队 {det['queue_risk']}" if det.get("queue_risk") else ""
            near = f" ｜ 离 {det['nearest_poi']}" if det.get("nearest_poi") else ""
            # 攻略层实证：逐点验证撞风控拿不到独立视频时，这是排序区分度的唯一来源
            gh = f" ｜ 攻略提及热度{det['guide_heat']}" if det.get("guide_heat") else ""
            lines.append(f"{r.get('rank', 0)}. **{r.get('ref_id')}** ｜ 推荐分 {r.get('score', 0):.2f}"
                         f" ｜ {avg}{n}{q}{near}{gh} ｜ [详情](#food-{r.get('ref_id')})")
        lines.append("")

    # 9 详情库（下钻落点，锚点与榜单一一对应）
    catalog = plan.get("catalog") or {}
    poi_cat, food_cat = catalog.get("poi") or {}, catalog.get("food") or {}
    digests = snap.get("digests") or {}
    if poi_cat or food_cat:
        lines += ["## 详情库", ""]
        for name, det in poi_cat.items():
            lines += [f"<a id=\"spot-{name}\"></a>", "", f"### {name}（{det.get('category', '景点')}）", ""]
            # 贴士/美食/热度证据行由 snap 快照只读补齐，故先取出来参与空壳判定
            extra_p = (snap.get("profiles") or {}).get(name) or {}
            hrow = next((h for h in (snap.get("heat") or []) if h.get("spot") == name), None)
            if not _has_poi_content(det, extra_p, hrow, digests.get(name)):
                lines += [EMPTY_CARD_NOTE, ""]
                continue
            if det.get("summary"):
                lines.append(f"- 简介：{det['summary']}")
            lines.append(_ticket_line(det))
            if det.get("open_hours"):
                lines.append(f"- 开放时间：{det['open_hours']}")
            if det.get("close_day"):
                lines.append(f"- 闭馆日：{det['close_day']}")
            if det.get("booking_rule"):
                lines.append(f"- 预约：{det['booking_rule']}")
            dur = f"约 {det['duration_hours']} 小时" if det.get("duration_hours") else "见要点"
            lines.append(f"- 建议时长：{dur}｜最佳时段：{det.get('best_slot') or '—'}"
                         + ("｜全天型（宜独占一天）" if det.get("is_all_day") else ""))
            if det.get("highlights"):
                lines.append(f"- 亮点：{'；'.join(det['highlights'])}")
            if det.get("guide_note"):
                lines.append(f"- 攻略提及：{det['guide_note']}（来自城市高赞攻略视频，非独立实测）")
            if det.get("photo_spots"):
                lines.append(f"- 打卡点：{'；'.join(det['photo_spots'])}")
            # 旧详情卡的贴士/美食/热度证据行（extra_p 与 hrow 已在循环开头取出）
            if extra_p.get("tips"):
                lines.append(f"- 贴士：{'；'.join(extra_p['tips'])}")
            if extra_p.get("food"):
                lines.append(f"- 美食：{'；'.join(extra_p['food'])}")
            if hrow:
                senti = f" · 情感 {hrow['sentiment']}" if hrow.get("sentiment") else ""
                lines.append(f"- 热度证据：视频 {hrow.get('videos', 0)} 条 · 点赞 {hrow.get('likes', 0)}"
                             f" · 评论 {hrow.get('comments', 0)}{senti}")
            for pit in det.get("pitfalls") or []:
                src = f"（来源：{pit['source']}）" if pit.get("source") else ""
                lines.append(f"- ❌ 别踩坑：{pit.get('text')}{src}")
            lines += _digest_lines(digests.get(name))
            urls = det.get("sources") or []
            if urls:
                links = " ".join(f"[来源{i + 1}]({u})" for i, u in enumerate(urls[:6]))
                lines.append(f"- 信息溯源：{links}")
            lines.append("")
        for name, det in food_cat.items():
            lines += [f"<a id=\"food-{name}\"></a>", "", f"### 🍜 {name}", ""]
            extra_f = (snap.get("foods") or {}).get(name) or {}
            if not _has_food_content(det, extra_f, digests.get(name)):
                lines += [EMPTY_CARD_NOTE, ""]
                continue
            if det.get("avg_price") is not None:
                n = f"（{det['price_samples_n']} 个样本）" if det.get("price_samples_n") else ""
                lines.append(f"- 人均：{det['avg_price']:.0f} 元（{det.get('avg_price_nature', '')}）{n}")
            else:
                lines.append("- 人均：待核实")
            if det.get("signature_dishes"):
                lines.append(f"- 招牌/推荐：{'；'.join(det['signature_dishes'])}")
            if det.get("guide_note"):
                lines.append(f"- 攻略提及：{det['guide_note']}（来自城市高赞攻略视频，非独立实测）")
            for pit in det.get("pitfalls") or []:
                lines.append(f"- ❌ 别踩坑：{pit.get('text')}")
            if det.get("queue_risk"):
                lines.append(f"- 排队风险：{det['queue_risk']}")
            if det.get("nearest_poi"):
                lines.append(f"- 离最近行程点：{det['nearest_poi']}")
            if extra_f.get("tips"):      # extra_f 已在循环开头取出
                lines.append(f"- 贴士：{'；'.join(extra_f['tips'])}")
            lines += _digest_lines(digests.get(name))
            urls = det.get("sources") or []
            if urls:
                links = " ".join(f"[来源{i + 1}]({u})" for i, u in enumerate(urls[:6]))
                lines.append(f"- 信息溯源：{links}")
            lines.append("")

    # 10 合规与时效声明 + 来源
    sources = (plan.get("appendix") or {}).get("sources") or []
    if sources:
        lines += ["## 数据来源", ""]
        lines += [f"- [{i + 1}]({u})" for i, u in enumerate(sources[:40])]
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# —— HTML：TripPlan → 既有模板 context（零计算投影） ——

def _decision_rows_for_template(decision_table: list[dict]) -> list[dict]:
    rows = []
    for r in decision_table or []:
        state = r.get("state") or "未定"
        hs = r.get("heat_score")
        rows.append({
            "name": r.get("name"), "category": r.get("category"), "sources": r.get("sources"),
            "evidence": r.get("evidence"), "state": state,
            "state_icon": _STATE_ICONS.get(state, "❔"),
            "heat": f"{hs:.2f}" if hs else "—",
            "trend": r.get("heat_trend") or "—",
            "mkt": f"{r['mkt_ratio']:.0%}" if r.get("mkt_ratio") else "—",
            "reason": r.get("reason") or "—",
        })
    return rows


# —— 行程全景图：纯几何投影（把 itinerary 摆到一张平面图上） ——

# 画布坐标系（逻辑尺寸 1000×620；模板用 aspect-ratio 等比缩放，故坐标与像素无关）
_MAP_W, _MAP_H = 1000.0, 620.0
_MAP_PAD_X = 86.0          # 左右内边距：给序号徽标与长景点名留位
# 垂直三带（上 / 中 / 下）。节点圆点直径约 42px，其下还有"景点名 + 时长"两行
# （约 46px），故 y 不可贴边。取值按"A 型路线"排布：
#   1 号点在上带 → 逐点下沉到中带、下带 → 再回到上带，呈山峰状；
#   上带 190（圆点顶 169，让开 top:8 的日标签）／下带 440（标签底约 503，留底部呼吸）。
_MAP_TOP_Y, _MAP_MID_Y, _MAP_BOT_Y = 190.0, 312.0, 440.0
_MAP_DAY_GAP = 30.0        # 天与天之间的横向间隔


def _map_layout(itinerary: list[dict]) -> dict:
    """把行程摆成平面图上的节点（纯函数，可离线断言）。

    设计取舍：**不表示真实地理**（报告里没有可靠经纬度，硬凑方位会骗人），
    改为"按行程顺序排布"——同一天的点聚成一簇，簇内按时段从左到右，
    簇与簇之间留白并用日标签分隔。这样表达的语义是"顺序与节奏"而非"方位"，
    与产品定位（给个大概认知）一致：用户看图知道"先去哪、再去哪、一天几个点"。

    纵向用三带轮转（上→中→下→中→上…，按 **全局序号** 而非每日重置）：
    这样从第 1 个点到最后一个点是一条连绵起伏的线，而不是每天重复同一个
    山峰形状。全局轮转还有个附带好处——跨天衔接处高度自然错开，
    不会出现"第 1 天末尾与第 2 天开头并列同高"的割裂感。
    坐标同时输出绝对像素（给 SVG 连线）与百分比（给 HTML 节点绝对定位），模板零计算。
    """
    days = [d for d in (itinerary or []) if d and (d.get("blocks") or [])]
    if not days:
        return {"nodes": [], "edges": [], "width": 0, "height": 0, "days": []}

    counts = [len(d.get("blocks") or []) for d in days]
    total = sum(counts) or 1
    span = _MAP_W - 2 * _MAP_PAD_X - _MAP_DAY_GAP * (len(days) - 1)
    # 上行（上→中→下）与下行（下→中→上）交替，避免锯齿状来回跳
    bands = (_MAP_TOP_Y, _MAP_MID_Y, _MAP_BOT_Y)

    nodes: list[dict] = []
    day_bands: list[dict] = []
    cursor = _MAP_PAD_X
    idx = 0
    for d in days:
        blocks = d.get("blocks") or []
        # 本天占据的横向宽度（按点位数加权，最少给 130 保证长标签不互相压）
        share = max(span * (len(blocks) / total) if total else span, 130.0)
        band_x0 = cursor
        step = share / len(blocks) if blocks else 0
        for bi, b in enumerate(blocks):
            idx += 1
            x = band_x0 + step * (bi + 0.5)
            # 全局序号轮转：0→上带, 1→中带, 2→下带, 3→下带, 4→中带, 5→上带…（镜像往复）
            period = len(bands) * 2 - 2          # 4：上中下中
            k = (idx - 1) % period
            y = bands[k if k < len(bands) else period - k]
            nodes.append({
                "n": idx,                                   # 全局序号（图上徽标）
                "day": d.get("day"),
                "seq_in_day": bi + 1,
                "spot": b.get("spot") or "",
                "slot": b.get("slot") or "",
                "duration": b.get("duration") or "",
                # 双坐标系：SVG 用绝对像素，节点用百分比（绝对定位）
                "x": round(x, 2), "y": round(y, 2),
                "x_pct": round(x / _MAP_W * 100, 2),
                "y_pct": round(y / _MAP_H * 100, 2),
                "has_detail": False,                        # 由调用方按 profiles 补齐
            })
        day_bands.append({
            "day": d.get("day"), "count": len(blocks),
            "x_pct": round((band_x0 + share / 2) / _MAP_W * 100, 2),
        })
        cursor = band_x0 + share + _MAP_DAY_GAP

    edges = [{"from": nodes[i]["n"], "to": nodes[i + 1]["n"],
              "x1": nodes[i]["x"], "y1": nodes[i]["y"],
              "x2": nodes[i + 1]["x"], "y2": nodes[i + 1]["y"],
              "same_day": nodes[i]["day"] == nodes[i + 1]["day"]}
             for i in range(len(nodes) - 1)]

    return {"nodes": nodes, "edges": edges, "width": _MAP_W, "height": _MAP_H,
            "days": day_bands}


def render_html(plan: dict) -> str:
    """把 TripPlan 投影成 templates/trip_report.html 的 context（只读，零计算）。"""
    meta = plan.get("meta") or {}
    snap = plan.get("snap") or {}
    catalog = plan.get("catalog") or {}
    poi_cat, food_cat = catalog.get("poi") or {}, catalog.get("food") or {}
    # 概览缺省给最小可用版（模板直接读 days/spots 等键，缺了不崩、显式 0）
    overview = {"days": meta.get("days"), "spots": len(poi_cat), "foods": len(food_cat),
                "slots": sum(len(d.get("blocks") or []) for d in plan.get("itinerary") or []),
                "highlights": 0, "pitfalls": 0}
    overview.update(snap.get("overview") or {})
    # profiles/foods 快照：注入 catalog 同源票价（R12 的「同一事实」在 HTML 的唯一出口）
    profiles: dict = {}
    for name, p in (snap.get("profiles") or {}).items():
        det = poi_cat.get(name) or {}
        profiles[name] = {**p, "ticket_price": det.get("ticket_price"),
                          "ticket_nature": det.get("ticket_nature")}
    foods: dict = {}
    for name, p in (snap.get("foods") or {}).items():
        det = food_cat.get(name) or {}
        foods[name] = {**p, "avg_price": det.get("avg_price"),
                       "avg_price_nature": det.get("avg_price_nature")}
    plan_shaped = {"summary_note": snap.get("summary_note") or "",
                   "guide_note": snap.get("guide_note") or "",
                   # 行程被系统修正过（去重/搬移）就必须对用户可见（不静默改行程）
                   "dedupe_note": snap.get("dedupe_note") or "",
                   "rebalance_note": snap.get("rebalance_note") or "",
                   # 模板只读不算：槽位票价文案在此算好（与 MD、详情卡同一口径）
                   "days": [{"day": d.get("day"),
                             "slots": [{**b, "cost_label": slot_price_labels(b)[1]}
                                       for b in (d.get("blocks") or [])]}
                            for d in plan.get("itinerary") or []]}
    spot_sources = {n: list(d.get("sources") or []) for n, d in poi_cat.items()}
    food_sources = {n: list(d.get("sources") or []) for n, d in food_cat.items()}
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("trip_report.html")
    generated = meta.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    # 全景图节点：坐标由纯函数算好（模板零计算）；has_detail 决定是否可点开弹层
    trip_map = _map_layout(plan.get("itinerary") or [])
    for nd in trip_map["nodes"]:
        nd["has_detail"] = nd["spot"] in profiles
    return tpl.render(
        city=meta.get("city"), days=meta.get("days"), hotel=meta.get("stay"),
        plan=plan_shaped, profiles=profiles, spot_sources=spot_sources,
        geo_on=bool(snap.get("geo_on")),
        preferences=meta.get("prefs") or "", preference_mode=meta.get("preference_mode") or "",
        user_spots=snap.get("user_spots") or [],
        pitfall=snap.get("pitfall") or [], heat=snap.get("heat") or [],
        digests=snap.get("digests") or {}, foods=foods, food_sources=food_sources,
        overview=overview,
        legs=snap.get("legs") or [],
        decision_rows=_decision_rows_for_template(plan.get("decision_table") or []),
        quality=plan.get("quality") or None,
        checklist=build_checklist(plan),   # 出发前确认清单（归类已有事实，不产生新结论）
        trip_map=trip_map,                 # 行程全景图：平面节点坐标（纯几何，不表真实方位）
        generated=generated.replace("T", " ")[:16],
        total_slots=sum(len(d.get("blocks") or []) for d in plan.get("itinerary") or []),
    )
