"""极简 Markdown 子集 → HTML（攻略报告正文专用）。

为什么不用现成库：本项目 Python 端没有任何 markdown 依赖（实测 markdown /
markdown2 / mistune / commonmark 均未安装），而报告是**离线单文件产物**，
项目约定离线产物不引 JS 框架（因此也不能像站内那样用 marked.js）。为了一段
正文引入新依赖不划算，故只实现报告实际用到的语法子集。

子集边界按库内全部 3 份真实攻略报告正文统计得出，未出现的语法一律不支持：

    # / ## / ###     标题（映射 h3 / h4 / h5，让位于页面自身的 h1 / h2）
    - 项              无序列表
    1. 项             有序列表
    **粗体**          行内加粗
    `代码`            行内等宽
    [文本](url)       链接（仅放行 http/https，挡掉 javascript: 之类）
    （[8]）           引用编号 → 锚到来源条目 #src-8，点号可跳到来源

其余内容一律按纯文本输出——**宁可少解析，不可丢内容**。

安全：先转义再套格式。报告正文里的 claim 直接来自抖音评论与视频文案，
属不可信输入，绝不允许其注入 HTML。
"""
import html
import re

__all__ = ["md_to_html", "inline"]

# 标题级别映射：页面自己占 h1/h2，正文标题整体降级，避免与页面骨架打架
_HEAD_TAGS = {1: "h3", 2: "h4", 3: "h5"}

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^[-*]\s+(.*)$")
_OL = re.compile(r"^\d+[.)]\s+(.*)$")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CITE = re.compile(r"\[(\d{1,3})\]")
_SAFE_URL = re.compile(r"^https?://", re.I)
_STASH = re.compile(r"\x00(\d+)\x00")


def _link(m: re.Match) -> str:
    """链接：只放行 http/https。其余（如 javascript:）按原文输出，不当链接用。"""
    text, url = m.group(1), m.group(2)
    if not _SAFE_URL.match(url):
        return m.group(0)
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{text}</a>'


def _cite(m: re.Match) -> str:
    """引用编号 → 锚点，点击跳到「来源要点清单」对应条目（结论可溯源）。"""
    n = m.group(1)
    return f'<a class="cite" href="#src-{n}" title="查看第 {n} 条来源">{n}</a>'


def inline(text: str) -> str:
    """行内格式。**输入必须是已转义的文本**（转义在调用方完成）。

    代码片段先摘出来占位，跑完其余规则再还原——否则代码里的 `[8]` 会被
    误当成引用编号加上锚点。
    """
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE.sub(_stash, text)
    text = _LINK.sub(_link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _CITE.sub(_cite, text)
    return _STASH.sub(lambda m: f"<code>{stash[int(m.group(1))]}</code>", text)


def md_to_html(md: str) -> str:
    """把正文 Markdown 子集转成 HTML 片段。纯函数，无副作用。"""
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag: str | None = None
    fence: list[str] | None = None      # 围栏代码块（报告未用，防意外吞行）

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            body = "".join(f"<li>{it}</li>" for it in items)
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
        list_tag = None

    def flush_all() -> None:
        flush_para()
        flush_list()

    for raw in lines:
        s = raw.strip()

        if s.startswith("```"):
            if fence is None:
                flush_all()
                fence = []
            else:
                out.append("<pre><code>" + html.escape("\n".join(fence)) + "</code></pre>")
                fence = None
            continue
        if fence is not None:
            fence.append(raw)
            continue

        if not s:
            flush_all()
            continue

        m = _HEADING.match(s)
        if m:
            flush_all()
            lvl = min(len(m.group(1)), 3)          # ###### 及以上一律并到 h5
            tag = _HEAD_TAGS[lvl]
            out.append(f"<{tag}>{inline(html.escape(m.group(2)))}</{tag}>")
            continue

        m = _UL.match(s)
        if m:
            flush_para()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            items.append(inline(html.escape(m.group(1))))
            continue

        m = _OL.match(s)
        if m:
            flush_para()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            items.append(inline(html.escape(m.group(1))))
            continue

        # 引用行：报告头部与免责声明用 >，正文里按提示句呈现
        if s.startswith(">"):
            flush_list()
            para.append(f'<span class="md-quote">{inline(html.escape(s.lstrip(">").strip()))}</span>')
            continue

        # 表格等未支持语法：不解析也不丢弃，按纯文本行输出
        flush_list()
        para.append(inline(html.escape(s)))

    if fence is not None:                          # 未闭合围栏：当代码块输出，别丢内容
        out.append("<pre><code>" + html.escape("\n".join(fence)) + "</code></pre>")
    flush_all()
    return "\n".join(out)
