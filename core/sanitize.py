"""去标识化与文本清洗：评论数据入库前的唯一合规闸口。

上游页面里能看到很多用户字段（昵称、头像、ID、主页链接等），
但只有经过本文件的解析与白名单过滤，数据才允许进入下游存储。
"""
import re
import time
from datetime import datetime, timedelta

from core.models import Comment

_ALLOWED_FIELDS = {"text", "like_count", "is_author_reply", "time"}

# 抖音评论条目的界面噪音行（相对时间与绝对日期都认，属地一律丢弃）
_TIME_TOKEN = r"刚刚|昨天|前天|\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前|(?:\d{4}[-年])?\d{1,2}[-月]\d{1,2}日?"
_TIME_LINE_RE = re.compile(rf"^({_TIME_TOKEN})(?:·\S+)?$")
_LIKE_LINE_RE = re.compile(r"^[\d.]+万?$")
_EXPAND_RE = re.compile(r"^展开\d+条回复$")
_UI_WORDS = ("分享", "回复", "作者赞过")


def clean_comment(raw: dict) -> Comment:
    data = {k: v for k, v in raw.items() if k in _ALLOWED_FIELDS}
    return Comment(
        text=str(data.get("text", "")).strip(),
        like_count=data.get("like_count"),
        is_author_reply=bool(data.get("is_author_reply", False)),
        time=data.get("time"),
    )


def time_token_to_date(token: str, now: datetime | None = None) -> str | None:
    """把评论时间 token 换算成 YYYY-MM-DD（纯函数可测）。

    支持：刚刚/昨天/前天/X秒分钟小时天周月年前（按月=30天、年=365天近似）/
    MM-DD / YYYY-MM-DD / YYYY年M月D日；无法识别返回 None。属地等信息不在此处理（已在行级丢弃）。"""
    now = now or datetime.now()
    t = (token or "").strip()
    if t == "刚刚":
        return now.strftime("%Y-%m-%d")
    if t == "昨天":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if t == "前天":
        return (now - timedelta(days=2)).strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d+)\s*(秒|分钟|小时|天|周|个月|月|年)前", t)
    if m:
        n = int(m.group(1))
        days = {"秒": 0, "分钟": 0, "小时": 0, "天": n, "周": 7 * n,
                "个月": 30 * n, "月": 30 * n, "年": 365 * n}[m.group(2)]
        return (now - timedelta(days=days)).strftime("%Y-%m-%d")
    m = re.fullmatch(r"(?:(\d{4})[-年])?(\d{1,2})[-月](\d{1,2})日?", t)
    if m:
        try:
            return datetime(int(m.group(1) or now.year), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def timestamp_to_date(ts) -> str | None:
    """unix 秒级时间戳 -> YYYY-MM-DD（纯函数可测）。

    评论接口返回的是精确时间戳，比页面上的"X 天前"可靠（无需按当前时间反推）。
    只接受抖音上线（2016 年）至今 +1 天内的值，越界当脏数据返回 None。"""
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    if not 1467000000 <= n <= time.time() + 86400:
        return None
    return datetime.fromtimestamp(n).strftime("%Y-%m-%d")


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


def parse_comment_block(raw: str) -> tuple[str, int | None, bool, str | None]:
    """把评论区单个条目的整块 innerText 解析成 (纯评论文本, 点赞数, 是否作者回复, 评论日期)。

    抖音条目结构固定形如（\\n 为换行）：
        昵称 \n ... \n 正文... \n 时间·属地 \n 点赞数 \n 分享 \n 回复 [\n 展开N条回复]
    （主评论下若内联了回复预览，则以多个 "..." 分段）
    作者回复的昵称旁带"作者"徽标，在 innerText 中表现为独立的"作者"行。
    检测从严：只认独立成行的精确匹配，宁漏不误标。
    评论日期由时间行换算（只留 YYYY-MM-DD，属地在行级丢弃），识别不了给 None。

    以 "..." 为界切段，第 0 段是昵称——直接丢弃，个人信息不进入任何下游数据。
    若整个条目没有 "..." 分隔符，无法可靠区分昵称与正文时，按隐私优先原则
    丢弃首行（宁可损失个别正文，也绝不采集昵称）。
    """
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return "", None, False, None

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
    c_time: str | None = None
    while seg:
        line = seg[-1]
        if _EXPAND_RE.fullmatch(line) or line in _UI_WORDS:
            seg.pop()
        elif (m := _TIME_LINE_RE.fullmatch(line)):
            if c_time is None:  # 只认最靠近正文的时间行（回复预览可能带多条）
                c_time = time_token_to_date(m.group(1))
            seg.pop()
        elif _LIKE_LINE_RE.fullmatch(line):
            like = parse_count(line)
            seg.pop()
        else:
            break

    return " ".join(seg).strip(), like, is_author, c_time
