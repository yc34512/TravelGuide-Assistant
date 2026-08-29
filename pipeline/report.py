"""Reduce 阶段：汇总全部要点，生成带溯源的攻略报告正文。"""
from core.llm import chat_text

REPORT_SYSTEM = """你是资深旅游攻略编辑。基于带来源编号和置信度标注的信息要点，撰写一份实用的中文景点攻略（Markdown 格式）。

规则：
1. 只能使用给出的要点信息，禁止编造或用你自己的知识补充；
2. 按置信度把握语气：【多源一致】可平实陈述为事实；【单源】用谨慎语气，如"有博主提到""有网友反馈"；【存分歧】必须并列呈现双方说法并注明"存在不同说法"，不要擅自取舍；
3. 来源标注了"时效敏感"的信息，句末加"⚠️出发前请核实"；
4. 引用某个要点时，在其句末标注来源编号，如 [3]；
5. 按素材情况选择章节：概览、门票与预约、交通、游玩路线与打卡点、避雷与注意、美食；没有素材支撑的章节省略，禁止输出"暂无信息/建议自行查询"之类的填充段落；
6. 篇幅 500~900 字，语言自然、可直接阅读。"""


def _strip_code_fence(text: str) -> str:
    """部分模型爱把 Markdown 正文包进 ```markdown 围栏，剥掉防止破坏最终排版。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def synthesize_report(keyword: str, points: list[dict]) -> str:
    numbered = "\n".join(
        f"[{i + 1}] ({p['topic']}|{p.get('confidence', '单源')}{'|时效敏感' if p['time_sensitive'] else ''}) {p['claim']} —— {p['source']}"
        for i, p in enumerate(points)
    )
    body = chat_text(
        REPORT_SYSTEM,
        f"景点/主题：{keyword}\n\n信息要点：\n{numbered or '(无)'}",
        temperature=0.3,
    )
    return _strip_code_fence(body)
