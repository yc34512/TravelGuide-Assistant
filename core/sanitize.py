"""去标识化与文本清洗：评论数据入库前的唯一合规闸口。

上游页面里能看到很多用户字段（昵称、头像、ID、主页链接等），
但只有经过本文件的解析与白名单过滤，数据才允许进入下游存储。
"""
import re

from core.models import Comment

_ALLOWED_FIELDS = {"text", "like_count", "is_author_reply"}

# 抖音评论条目的界面噪音行
_TIME_LINE_RE = re.compile(r"^(刚刚|\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前)(?:·\S+)?$")
_LIKE_LINE_RE = re.compile(r"^[\d.]+万?$")
_EXPAND_RE = re.compile(r"^展开\d+条回复$")
_UI_WORDS = ("分享", "回复", "作者赞过")


def clean_comment(raw: dict) -> Comment:
    data = {k: v for k, v in raw.items() if k in _ALLOWED_FIELDS}
    return Comment(
        text=str(data.get("text", "")).strip(),
        like_count=data.get("like_count"),
        is_author_reply=bool(data.get("is_author_reply", False)),
    )


def parse_count(text: str | None) -> int | None:
    """把页面计数控还原成整数："1.2万" -> 12000，"856" -> 856。"""
    if not text:
        return None
    t = text.strip().replace(",", "").replace("，", "")
    m = re.search(r"([\d.]+)\s*万", t)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"\d+", t)
    return int(m.group(0)) if m else None


def parse_comment_block(raw: str) -> tuple[str, int | None, bool]:
    """把评论区单个条目的整块 innerText 解析成 (纯评论文本, 点赞数, 是否作者回复)。

    抖音条目结构固定形如：
        昵称 \n ... \n 正文... \n 时间·属地 \n 点赞数 \n 分享 \n 回复 [\n 展开N条回复]
    （主评论下若内联了回复预览，则以多个 "..." 分段）
    作者回复的昵称旁带"作者"徽标，在 innerText 中表现为独立的"作者"行。
    检测从严：只认独立成行的精确匹配，宁漏不误标。

    以 "..." 为界切段，第 0 段是昵称——直接丢弃，个人信息不进入任何下游数据。
    若整个条目没有 "..." 分隔符，无法可靠区分昵称与正文时，按隐私优先原则
    丢弃首行（宁可损失个别正文，也绝不采集昵称）。
    """
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return "", None, False

    is_author = any(l == "作者" for l in lines)

    if "..." in lines:
        rest = lines[lines.index("...") + 1:]
        if "..." in rest:
            # 主评论下内联了回复预览：只取主评论段；
            # 该段末行是下一条评论的昵称，按隐私优先原则一并丢弃
            seg = rest[: rest.index("...")]
            if len(seg) >= 2:
                seg = seg[:-1]
        else:
            seg = rest
    else:
        # 无分隔符时无法可靠区分昵称与正文，宁可丢首行也绝不采集昵称
        seg = lines[1:]

    like: int | None = None
    while seg:
        line = seg[-1]
        if _EXPAND_RE.fullmatch(line) or line in _UI_WORDS:
            seg.pop()
        elif _TIME_LINE_RE.fullmatch(line):
            seg.pop()
        elif _LIKE_LINE_RE.fullmatch(line):
            like = parse_count(line)
            seg.pop()
        else:
            break

    return " ".join(seg).strip(), like, is_author
