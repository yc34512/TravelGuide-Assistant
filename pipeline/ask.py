"""行程认知追问：基于已有行程档案回答用户的自然语言问题（绝不重新采集）。

产品定位决定了这个模块为什么存在：报告交付的是"关于目的地的大概"，
用户读完必然会有追问——"这个为什么值得去""带小孩合适吗""两个只能选一个选哪个"。
这些问题的答案**本来就都在档案里**（亮点/避雷/真实评价摘要/热度/注意事项/待确认清单），
所以追问只是"把已有信息按问题重新组织一遍"，而不是一次新的调研：

- 不触发任何采集（不碰 crawler / 不开浏览器 / 不耗频控预算）；
- 不重排行程、不改任何结论（纯解释层，只读 result）；
- 一次 LLM 调用，输入是压缩后的档案上下文。

分工：
- ``build_context``：把 TripPlan / 报告全文压成给模型看的档案（纯函数，可离线断言）；
- ``answer``：拼提示词并调用 ``core.llm.chat_text``（唯一有副作用的一步）。

防幻觉约束写死在 system 提示里：只依据档案回答，档案没有就说不知道，
涉及票价/开放时间等时效信息一律提醒以官方为准——与报告的"宁缺不编"保持同一口径。
"""
from __future__ import annotations

from core.llm import chat_text

# 上下文体积上限（字符）：中文约 1 字 ≈ 1 token，控制在 6k 以内，
# 既够放下一天的行程细节，又不会把调用成本推高。
_CTX_MAX_CHARS = 6000

# 单点档案里各列表最多带几条、单条最多多少字（避免一个点吃掉整个预算）
_MAX_LIST_ITEMS = 4
_MAX_ITEM_CHARS = 90

_SYSTEM = """你是这份旅游报告的解读助手。报告基于抖音公开攻略视频与真实评论整理而成，\
用户读完后就其中的内容向你追问。请遵守：

1. 只依据【报告档案】回答。档案里没有的，直接说"报告里没有这方面的信息"，
   再补一句"建议出发前自行确认"，绝不编造，也不要凭常识补充具体数字；
2. 回答简短（100~200 字），先给结论，再给依据；能引用档案里的原话或数据就引用；
3. 用户问"A 和 B 只能选一个"这类取舍时，必须给出明确选择并说明理由
   （依据档案里的亮点、避雷、真实评价、热度）；
4. 不要输出 Markdown 标题（#），可以用短句与短分点；不要客套开场白；
5. 涉及票价、开放时间、预约、排队等会变化的信息时，提醒以官方渠道为准。"""


def _clip(text, limit: int = _MAX_ITEM_CHARS) -> str:
    t = str(text or "").strip().replace("\n", " ")
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _brief_list(title: str, items, limit: int = _MAX_LIST_ITEMS) -> str:
    vals = [_clip(x) for x in (items or []) if str(x or "").strip()]
    if not vals:
        return ""
    joined = "；".join(vals[:limit])
    more = f"（另 {len(vals) - limit} 条略）" if len(vals) > limit else ""
    return f"    {title}：{joined}{more}\n"


def _mentioned_points(tp: dict, question: str, limit: int = 4) -> list[str]:
    """问题里点名的景点 —— 把档案预算集中到用户真正问的点上。"""
    names: list[str] = []
    seen: set[str] = set()
    catalog = tp.get("catalog") or {}
    for bucket in ("poi", "food"):
        for nm in (catalog.get(bucket) or {}):
            nm = str(nm).strip()
            if nm and nm not in seen:
                seen.add(nm)
                names.append(nm)
    for d in tp.get("itinerary") or []:
        for b in d.get("blocks") or []:
            nm = str(b.get("spot") or "").strip()
            if nm and nm not in seen:
                seen.add(nm)
                names.append(nm)
    # 长名优先，避免"山"这类单字误命中
    hits = [n for n in sorted(names, key=len, reverse=True) if n in question]
    return hits[:limit]


def _itinerary_skeleton(tp: dict) -> str:
    out: list[str] = []
    for d in tp.get("itinerary") or []:
        parts: list[str] = []
        for b in d.get("blocks") or []:
            seg = f"{b.get('slot') or ''} {b.get('spot') or ''}".strip()
            if b.get("duration"):
                seg += f"({b['duration']})"
            if seg:
                parts.append(seg)
        if parts:
            out.append(f"  第{d.get('day')}天：" + " → ".join(parts))
    return "\n".join(out)


def _point_dossier(tp: dict, name: str, deep: bool) -> str:
    """单个点的档案摘要。deep=True 时给全量字段（用户点名问的点）。"""
    snap = tp.get("snap") or {}
    cat = (tp.get("catalog") or {}).get("poi") or {}
    food_cat = (tp.get("catalog") or {}).get("food") or {}
    prof = (snap.get("profiles") or {}).get(name) or {}
    food_prof = (snap.get("foods") or {}).get(name) or {}
    det = cat.get(name) or food_cat.get(name) or {}
    lines = [f"- {name}"]
    if det.get("ticket_price") is not None:
        lines.append(f"    票价：{det['ticket_price']:.0f} 元")
    elif str(det.get("ticket_nature") or "").strip():
        nature = str(det["ticket_nature"]).strip()
        # nature 也是"待核实"时不再套括号（否则输出"待核实（待核实）"，本项目曾踩过）
        lines.append("    票价：待核实" if nature == "待核实"
                     else f"    票价：待核实（{_clip(nature, 30)}）")
    if det.get("open_hours"):
        lines.append(f"    开放时间：{_clip(det['open_hours'], 40)}")
    if prof.get("duration_hours"):
        lines.append(f"    建议时长：{prof['duration_hours']} 小时")
    if prof.get("best_time_slot"):
        lines.append(f"    最佳时段：{prof['best_time_slot']}")
    body = (
        _brief_list("亮点", prof.get("highlights") or (deep and food_prof.get("highlights")))
        + _brief_list("避雷", prof.get("avoid"))
        + _brief_list("美食", prof.get("food"))
        + _brief_list("打卡点", prof.get("photo_spots"))
        + _brief_list("贴士", prof.get("tips"))
    )
    if not body and not deep:
        body = _brief_list("亮点", prof.get("highlights"))
    lines.append(body.rstrip("\n") if body else "    （报告未收录该点的详细信息）")
    if deep:
        dg = (snap.get("digests") or {}).get(name) or {}
        if dg.get("verdict"):
            lines.append(f"    真实评价摘要：{_clip(dg['verdict'], 140)}")
            if dg.get("positive"):
                lines.append(f"      好评：{_clip(dg['positive'])}")
            if dg.get("negative"):
                lines.append(f"      差评：{_clip(dg['negative'])}")
            for q in (dg.get("quotes") or [])[:2]:
                lines.append(f"      评论原话：{_clip(q, 80)}")
        for pit in (det.get("pitfalls") or [])[:_MAX_LIST_ITEMS]:
            if isinstance(pit, dict) and pit.get("text"):
                lines.append(f"    避坑：{_clip(pit['text'])}")
    return "\n".join(lines) + "\n"


def _tp_context(tp: dict, question: str, max_chars: int) -> str:
    """行程报告：结构化档案上下文（要点名优先，其余给骨架 + 摘要）。"""
    meta = tp.get("meta") or {}
    snap = tp.get("snap") or {}
    head = [
        f"城市：{meta.get('city') or '—'}｜天数：{meta.get('days') or '—'}｜"
        f"住宿：{meta.get('stay') or '未指定'}",
        f"生成时间：{meta.get('generated_at') or '—'}",
    ]
    if meta.get("prefs"):
        head.append(f"用户偏好：{meta['prefs']}")
    if snap.get("summary_note"):
        head.append(f"规划说明：{_clip(snap['summary_note'], 160)}")

    skel = _itinerary_skeleton(tp)
    hits = _mentioned_points(tp, question)

    parts = ["【报告档案】", "■ 基本信息", "\n".join(head)]
    if skel:
        parts += ["", "■ 行程骨架", skel]

    # 用户点名的点：给全量档案（这是回答质量的关键）
    if hits:
        parts += ["", "■ 你问到的点（完整档案）"]
        for nm in hits:
            parts.append(_point_dossier(tp, nm, deep=True))

    # 行程内其余点：只给一句话摘要，避免预算被无关点吃掉
    rest: list[str] = []
    for d in tp.get("itinerary") or []:
        for b in d.get("blocks") or []:
            nm = str(b.get("spot") or "").strip()
            if nm and nm not in hits and nm not in rest:
                rest.append(nm)
    if rest:
        parts += ["", "■ 行程内其它点（摘要）"]
        for nm in rest:
            parts.append(_point_dossier(tp, nm, deep=False))

    pitfall = snap.get("pitfall") or []
    if pitfall:
        parts += ["", "■ 避坑专题（附评论原文与来源）"]
        for row in pitfall[:5]:
            seg = f"- {_clip(row.get('claim'), 100)}"
            if row.get("quote"):
                seg += f"｜评论原话：{_clip(row['quote'], 80)}"
            parts.append(seg)

    heat = snap.get("heat") or []
    if heat:
        parts += ["", "■ 热度（近90天抖音数据）"]
        parts.append("；".join(
            f"{r.get('spot') or r.get('ref_id')} {r.get('score', 0):.2f}（{r.get('trend') or '—'}）"
            for r in heat[:6]))

    # 复用「出发前请自行确认」清单：追问也应当知道"哪些还没确认"，避免把不确定说成确定
    try:
        from pipeline.checklist import build_checklist
        cl = build_checklist(tp)
        if cl.get("groups"):
            parts += ["", "■ 尚未确认的信息（回答时不得当成已知事实）"]
            for g in cl["groups"]:
                for it in g["rows"][:3]:
                    parts.append(f"- [{g['kind']}] {it['name']}：{_clip(it['detail'], 70)}")
            if cl.get("footnote"):
                parts.append(cl["footnote"])
            if cl.get("cutoff"):
                parts.append(f"（以上信息截至 {cl['cutoff']}）")
    except Exception:
        pass

    return "\n".join(parts)[:max_chars]


def build_context(result: dict | None, question: str, max_chars: int = _CTX_MAX_CHARS) -> str:
    """把任务结果压成给模型看的档案上下文（纯函数）。

    - 行程报告（有 trip_plan）：结构化摘要，问题点名的景点给完整档案；
    - 攻略报告（只有 markdown）：取正文并截断（保留开头，报告结论集中在前部）。
    """
    result = result or {}
    tp = result.get("trip_plan")
    if tp:
        return _tp_context(tp, question or "", max_chars)
    md = str(result.get("markdown") or "").strip()
    if md:
        head = min(len(md), max_chars)
        tail_note = "\n（正文过长已截断，仅保留前部）" if len(md) > head else ""
        return "【报告全文】\n" + md[:head] + tail_note
    return ""


def _history_block(history, limit: int = 2) -> str:
    """把最近几轮问答带进上下文（多轮追问的连续性）。"""
    lines: list[str] = []
    for h in (history or [])[-limit:]:
        q = _clip((h or {}).get("q"), 120)
        a = _clip((h or {}).get("a"), 200)
        if q:
            lines.append(f"用户：{q}")
        if a:
            lines.append(f"你：{a}")
    return "\n".join(lines)


def answer(result: dict | None, question: str, history=None) -> str:
    """回答一个追问（唯一有副作用的一步：调用一次 LLM）。

    档案为空时直接给出可执行的提示，不空跑一次调用。
    """
    q = (question or "").strip()
    if not q:
        raise ValueError("问题不能为空")
    ctx = build_context(result, q)
    if not ctx:
        raise ValueError("这份报告没有可供追问的档案（可能已过期或未生成结果）")
    user = f"{ctx}\n\n【此前的追问】\n{_history_block(history)}\n\n【用户追问】\n{q}" \
        if _history_block(history) else f"{ctx}\n\n【用户追问】\n{q}"
    return chat_text(_SYSTEM, user, temperature=0.3).strip()
