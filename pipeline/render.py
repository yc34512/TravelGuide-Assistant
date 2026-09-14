"""报告组装：头部统计 + 速览清单 + LLM 正文 + 来源清单 + 免责声明。

同时产出两种产物，共用同一套分栏与徽章口径（避免两份口径以后漂移）：
  · render_report      → Markdown 留档（可下载、可 diff、便于排查）
  · render_report_html → HTML 图文版（给人读：杂志排版 + 三色速览卡 + 可溯源来源清单）
"""
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.models import VideoItem
from pipeline import mdmini

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"

# 速览分栏口径：存分歧优先于立场——有分歧就不替你下结论，单独归到「待你判断」
_GLANCE_ORDER = ("rec", "avoid", "mid")
_GLANCE_META = {
    "rec": ("✅", "值得做"),
    "avoid": ("❌", "别踩坑"),
    "mid": ("⚠️", "有争议，出发前自行判断"),
}
# 立场 → 展示与配色类名（CSS 里 st-rec / st-avoid / st-mid）
_STANCE_CLS = {"推荐": "st-rec", "避雷": "st-avoid", "中性": "st-mid"}
_STANCE_ICON = {"推荐": "✅", "避雷": "❌", "中性": "➖"}

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


def _glance_buckets(points: list[dict]) -> list[tuple[str, list[tuple[int, dict]]]]:
    """速览分栏：返回 [(栏位, [(序号, 要点), ...]), ...]，只含非空栏。

    序号是**全量要点里的 1-based 下标**，不是栏内序号——正文与来源清单里的
    [n] 引用用的就是这个编号，三处必须一致，否则点引用会跳到错的条目。
    """
    rec: list[tuple[int, dict]] = []
    avoid: list[tuple[int, dict]] = []
    disputed: list[tuple[int, dict]] = []
    for i, p in enumerate(points, 1):
        if p.get("confidence") == "存分歧":
            disputed.append((i, p))
        elif p.get("stance") == "推荐":
            rec.append((i, p))
        elif p.get("stance") == "避雷":
            avoid.append((i, p))
    buckets = {"rec": rec, "avoid": avoid, "mid": disputed}
    return [(k, buckets[k]) for k in _GLANCE_ORDER if buckets[k]]


def _quick_glance(points: list[dict]) -> str:
    """结构化速览：由代码直接从要点聚合（不依赖 LLM，永不缺席）。
    三栏：值得做（推荐且无分歧）/ 别踩坑（避雷且无分歧）/ 有争议（存分歧）。"""
    buckets = _glance_buckets(points)
    if not buckets:
        return ""
    parts = ["## 速览：可取 / 不可取", ""]
    for key, rows in buckets:
        icon, title = _GLANCE_META[key]
        parts += [f"**{icon} {title}**（{len(rows)} 条）",
                  *[f"- {p['claim']}（[{i}]）" for i, p in rows], ""]
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


# 立场 → 抬头统计 chip 的配色类名（CSS 里 .stance-chip.rec / .avoid / .mid）
_STANCE_CHIP = {"推荐": "rec", "避雷": "avoid", "中性": "mid"}


def render_report_html(keyword: str, body_md: str, items: list[VideoItem],
                       points: list[dict]) -> str:
    """攻略报告图文版（HTML 单文件，离线可用）。

    与 render_report（Markdown 留档）同源同口径：速览三栏、置信度徽章、引用编号
    全部复用上面同一批函数，两份产物不会各说各话。

    正文是 LLM 产出的 Markdown，交给 pipeline.mdmini 转 HTML——本项目 Python 端
    没有 markdown 依赖，离线产物又不引 JS 框架（不能用 marked.js），故只支持
    报告实际用到的语法子集，未支持的语法按纯文本输出（宁可少解析，不可丢内容）。
    """
    total_comments = sum(len(i.comments) for i in items)

    stance_dist: dict[str, int] = {}
    for p in points:
        key = p.get("stance", "中性")
        stance_dist[key] = stance_dist.get(key, 0) + 1

    glance_groups = [
        {
            "cls": key,
            "icon": _GLANCE_META[key][0],
            "title": _GLANCE_META[key][1],
            "rows": [{"n": i, "claim": p.get("claim", "")} for i, p in rows],
        }
        for key, rows in _glance_buckets(points)
    ]

    source_rows = [
        {
            "n": i,
            "topic": p.get("topic", "其他"),
            "badge": _conf_badge(p),
            "conf_cls": f"conf-{p['conf_level']}" if p.get("conf_level") else "",
            "stance": p.get("stance", "中性"),
            "stance_cls": _STANCE_CLS.get(p.get("stance", "中性"), "st-mid"),
            "time_sensitive": bool(p.get("time_sensitive")),
            "claim": p.get("claim", ""),
            "source": p.get("source", ""),
        }
        for i, p in enumerate(points, 1)
    ]

    # 立场分布固定按 推荐/避雷/中性 排列（dict 插入序会随要点顺序漂移，不便读）
    ordered_stances = [k for k in ("推荐", "避雷", "中性") if k in stance_dist]
    ordered_stances += [k for k in stance_dist if k not in ordered_stances]

    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR),
                      autoescape=select_autoescape(["html"]))
    return env.get_template("spot_report.html").render(
        keyword=keyword,
        generated=f"{datetime.now():%Y-%m-%d %H:%M}",
        video_count=len(items),
        comment_count=total_comments,
        point_count=len(points),
        # 「多源印证」= 不止一条独立来源支持，比总条数更能体现可信度
        multi_src_count=sum(1 for p in points if (p.get("n_sources") or 1) > 1),
        stance_rows=[
            {
                "name": k,
                "n": stance_dist[k],
                "chip_cls": _STANCE_CHIP.get(k, "mid"),
                "icon": _STANCE_ICON.get(k, "➖"),
            }
            for k in ordered_stances
        ],
        glance_groups=glance_groups,
        guide_html=mdmini.md_to_html(body_md),
        source_rows=source_rows,
        video_rows=[{"video_id": it.video_id, "url": it.url} for it in items],
        disclaimer_html=mdmini.md_to_html(DISCLAIMER),
    )
