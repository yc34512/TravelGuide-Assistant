"""Reduce 阶段：汇总全部要点，生成带溯源的攻略报告正文。"""
from core.llm import chat_text
from core.evidence import usable_points

REPORT_SYSTEM = """你是资深旅游攻略编辑。基于带来源编号、置信度与立场标注的信息要点，撰写一份实用的中文景点攻略（Markdown 格式）。

要点格式：[编号] (主题|置信度|立场[|时效敏感]) 一句话要点 —— 来源链接；立场含义：推荐=值得做，避雷=别踩坑，中性=客观信息。

规则：
1. 只能使用给出的要点信息，禁止编造或用你自己的知识补充；
2. 按置信度把握语气：【多源一致】可平实陈述为事实；【单源】用谨慎语气，如"有博主提到""有网友反馈"；【存分歧】必须并列呈现双方说法并注明"存在不同说法"，不要擅自取舍；
3. 立场决定选材：推荐类要点汇入"值得做"的内容，避雷类要点汇入"别踩坑"的内容；报告必须同时覆盖这两面，只说好话或只挑刺都是失职；
4. 来源标注了"时效敏感"的信息，句末加"⚠️出发前请核实"；
5. 引用某个要点时，在其句末标注来源编号，如 [3]；
6. 按素材情况选择章节：概览、值得做、别踩坑、门票与预约、交通、游玩路线与打卡点、美食；没有素材支撑的章节省略，禁止输出"暂无信息/建议自行查询"之类的填充段落；"值得做"与"别踩坑"两章只要对应立场有要点就必须输出；
7. 篇幅 500~900 字，语言自然、可直接阅读。"""


def _strip_code_fence(text: str) -> str:
    """部分模型爱把 Markdown 正文包进 ```markdown 围栏，剥掉防止破坏最终排版。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def synthesize_report(keyword: str, points: list[dict]) -> str:
    pending = [p for p in points if p.get("support_status") != "supported"]
    points = usable_points(points)
    waiting = ""
    if pending:
        waiting = "\n\n## 待确认的素材结论\n\n" + "\n".join(
            f"- 待确认：{p.get('claim', '')}（{p.get('support_reason') or '缺少原文支持性确认'}）"
            for p in pending)
    if not points:
        return "本次没有通过原文支持性复核的攻略要点，不能据此确认票价或开放安排。" + waiting
    numbered = "\n".join(
        f"[{i + 1}] ({p['topic']}|{p.get('confidence', '单源')}|{p.get('stance', '中性')}"
        f"{'|时效敏感' if p['time_sensitive'] else ''}) {p['claim']} —— {p['source']}"
        for i, p in enumerate(points)
    )
    body = chat_text(
        REPORT_SYSTEM,
        f"景点/主题：{keyword}\n\n信息要点：\n{numbered or '(无)'}",
        temperature=0.3,
    )
    return _strip_code_fence(body) + waiting
