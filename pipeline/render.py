"""报告组装：头部统计 + LLM 正文 + 来源清单 + 免责声明。"""
from datetime import datetime

from core.models import VideoItem

DISCLAIMER = """> ℹ️ 本报告由 AI 汇总抖音公开视频与评论区信息生成，仅供参考，不构成任何承诺。
> 价格、开放时间、政策等时效信息可能已变化，**出发前请务必通过官方渠道核实**。
> 如内容涉及侵权或需删除，可按来源清单中的视频 ID 定位并下架。"""


def render_report(keyword: str, body_md: str, items: list[VideoItem], points: list[dict]) -> str:
    total_comments = sum(len(i.comments) for i in items)

    point_lines = "\n".join(
        f"{i + 1}. ({p['topic']}{' · ⚠️时效敏感' if p['time_sensitive'] else ''}) {p['claim']} —— 来源：{p['source']}"
        for i, p in enumerate(points)
    )
    video_lines = "\n".join(f"- [{it.video_id}]({it.url})" for it in items)

    return f"""# 《{keyword}》旅游攻略报告

> 生成时间：{datetime.now():%Y-%m-%d %H:%M}
> 数据来源：抖音公开内容 · {len(items)} 条视频 · {total_comments} 条评论
> 合规说明：评论仅保留文本与点赞数，不含任何用户个人信息；报告为分析引用，原始内容请通过链接访问。

{body_md}

## 来源要点清单

{point_lines or "（无）"}

## 采集视频清单

{video_lines or "（无）"}

{DISCLAIMER}
"""
