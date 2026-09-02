"""报告组装：头部统计 + 速览清单 + LLM 正文 + 来源清单 + 免责声明。"""
from datetime import datetime

from core.models import VideoItem

DISCLAIMER = """> ℹ️ 本报告由 AI 汇总抖音公开视频与评论区信息生成，仅供参考，不构成任何承诺。
> 价格、开放时间、政策等时效信息可能已变化，**出发前请务必通过官方渠道核实**。
> 如内容涉及侵权或需删除，可按来源清单中的视频 ID 定位并下架。"""


def _conf_badge(p: dict) -> str:
    """置信度徽章：高/中/低置信度 + 独立来源数（未量化时回退旧三级标签）。"""
    level = p.get("conf_level")
    if not level:
        return p.get("confidence", "单源")
    n = p.get("n_sources") or 1
    return f"{level}·{n}来源" if n > 1 else level


def _quick_glance(points: list[dict]) -> str:
    """结构化速览：由代码直接从要点聚合（不依赖 LLM，永不缺席）。
    三栏：值得做（推荐且无分歧）/ 别踩坑（避雷且无分歧）/ 有争议（存分歧）。"""
    rec, avoid, disputed = [], [], []
    for i, p in enumerate(points, 1):
        line = f"- {p['claim']}（[{i}]）"
        if p.get("confidence") == "存分歧":
            disputed.append(line)
        elif p.get("stance") == "推荐":
            rec.append(line)
        elif p.get("stance") == "避雷":
            avoid.append(line)
    if not (rec or avoid or disputed):
        return ""
    parts = ["## 速览：可取 / 不可取", ""]
    if rec:
        parts += [f"**✅ 值得做**（{len(rec)} 条）", *rec, ""]
    if avoid:
        parts += [f"**❌ 别踩坑**（{len(avoid)} 条）", *avoid, ""]
    if disputed:
        parts += [f"**⚠️ 有争议，出发前自行判断**（{len(disputed)} 条）", *disputed, ""]
    return "\n".join(parts)


def render_report(keyword: str, body_md: str, items: list[VideoItem], points: list[dict]) -> str:
    total_comments = sum(len(i.comments) for i in items)
    stance_dist = {}
    for p in points:
        stance_dist[p.get("stance", "中性")] = stance_dist.get(p.get("stance", "中性"), 0) + 1

    point_lines = "\n".join(
        f"{i + 1}. ({p['topic']} · {_conf_badge(p)} · {p.get('stance', '中性')}"
        f"{' · ⚠️时效敏感' if p['time_sensitive'] else ''}) {p['claim']} —— 来源：{p['source']}"
        for i, p in enumerate(points)
    )
    video_lines = "\n".join(f"- [{it.video_id}]({it.url})" for it in items)
    glance = _quick_glance(points)
    stance_str = "、".join(f"{k} {v}" for k, v in stance_dist.items()) or "无"

    return f"""# 《{keyword}》旅游攻略报告

> 生成时间：{datetime.now():%Y-%m-%d %H:%M}
> 数据来源：抖音公开内容 · {len(items)} 条视频 · {total_comments} 条评论
> 要点立场分布：{stance_str}
> 合规说明：评论仅保留文本与点赞数，不含任何用户个人信息；报告为分析引用，原始内容请通过链接访问。

{glance}

{body_md}

## 来源要点清单

{point_lines or "（无）"}

## 采集视频清单

{video_lines or "（无）"}

{DISCLAIMER}
"""
