"""热度分析与避坑专题（P0）。

热度指数三因子（全部来自结构化采集数据，不依赖视频内容）：
- 互动热度 40%：视频点赞总量（log 归一，防头部碾压）；
- 评论密度 30%：评论数 / 视频数（讨论活跃度的代理指标）；
- 新鲜度 30%：近 90 天发布视频的占比（"最近突然火了"的信号）。
另附两个可信度指标：
- 营销号占比：文案命中营销话术的视频占比（≥50% 的热度需谨慎看待）；
- 评论情感趋势：近 30 天评论好评率与更早对比（判断是不是越做越差）。

避坑专题：避雷立场要点汇总，每条附评论原文引用 + 来源 + 置信度，
按量化置信度评分降序（高置信度排前，同分再按语义标签排）。
纯函数，独立可测。
"""
import math
from datetime import datetime, timedelta

from pipeline.candidates import is_marketing
from pipeline.verify import CONF_LOW, CONF_MID

FRESH_DAYS = 90
DIGEST_MAX = 12  # 避坑专题条数上限，多了没人看

# 趋势判定阈值（初始常量，集中在此便于调参）：
# 本周最火：近 7 天发布占比高（新鲜内容井喷）；长盛不衰：热度高但新增不多（经典常青）；
# 正在降温：内容主要在 60 天前且近 7 天几乎无新增，且综合热度也不高。
HOT_FRESH7 = 0.3
HOT_SCORE = 0.6
COOL_OLD60 = 0.5
COOL_FRESH7 = 0.1

# 归一化锚点：抖音景点类视频的常见量级（超过即视为满分档）
_LIKE_ANCHOR = 50000.0
_DENSITY_ANCHOR = 50.0


def _log_norm(value: float, anchor: float) -> float:
    """对数归一到 0~1：anchor 处取 1.0，防头部数据碾压长尾。"""
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(anchor))


def heat_index(items: list, now: datetime | None = None) -> dict:
    """计算单个候选的热度画像。items 为 VideoItem 列表。

    返回 {"score": 0~1, "trend", "videos", "likes", "comments", "fresh_ratio",
          "marketing": 营销号视频数, "mkt_ratio": 营销号占比}。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=FRESH_DAYS)
    n = len(items)
    likes = sum(it.like_count or 0 for it in items)
    comments = sum(len(it.comments) for it in items)
    marketing = sum(1 for it in items if is_marketing(getattr(it, "description", "") or ""))
    fresh = 0
    for it in items:
        try:
            if it.publish_time and datetime.strptime(it.publish_time[:10], "%Y-%m-%d") >= cutoff:
                fresh += 1
        except ValueError:
            pass
    fresh_ratio = fresh / n if n else 0.0
    score = (
        0.4 * _log_norm(likes, _LIKE_ANCHOR)
        + 0.3 * _log_norm(comments / n if n else 0.0, _DENSITY_ANCHOR)
        + 0.3 * fresh_ratio
    )
    return {
        "score": round(score, 3),
        "trend": "近期热度上升" if (fresh_ratio >= 0.4 and score >= 0.3) else "平稳",
        "videos": n,
        "likes": likes,
        "comments": comments,
        "fresh_ratio": round(fresh_ratio, 2),
        "marketing": marketing,
        "mkt_ratio": round(marketing / n, 2) if n else 0.0,
    }


# —— 评论情感趋势：关键词词典判定，不另走 LLM（评论量大，纯函数可测可调参）——
# 负面词先判："不好吃"含"好吃"、"不推荐"含"推荐"，先命中负面则不会被正面误判
NEG_WORDS = ("不好", "不值", "不推荐", "不划算", "坑", "宰", "难吃", "难看", "失望", "后悔",
             "避雷", "劝退", "差劲", "垃圾", "态度差", "服务差", "踩雷", "智商税", "脏",
             "太贵", "好贵", "又贵", "排队太久", "套路", "恶心", "糟糕", "拉胯", "别去", "干呕")
POS_WORDS = ("好看", "好吃", "值得", "推荐", "惊艳", "震撼", "喜欢", "划算", "实惠", "良心",
             "绝了", "yyds", "不错", "满意", "舒服", "热情", "干净", "方便", "超值", "性价比",
             "好玩", "开心", "很美", "太美", "景美", "赞", "棒", "划算哭", "良心价")

# 趋势判定：近 30 天好评率 - 更早好评率，±10pp 为升/降阈值；两侧有效样本各至少 5 条
SENTIMENT_WINDOW_DAYS = 30
SENTIMENT_MIN_N = 5
SENTIMENT_THRESHOLD = 0.1


def classify_comment(text: str) -> int:
    """关键词情感判定：负面优先（反词包含关系），命中负面 -1 / 正面 1 / 中性 0。纯函数可测。"""
    t = text or ""
    for w in NEG_WORDS:
        if w in t:
            return -1
    for w in POS_WORDS:
        if w in t:
            return 1
    return 0


def sentiment_trend(comments: list, now: datetime | None = None) -> dict:
    """评论情感趋势：近 30 天好评率 vs 更早好评率（判断是不是越做越差）。纯函数可测。

    comments 为 Comment 列表（需 .text 与 .time；time 缺失的无法定位时间窗，跳过）。
    好评率 = 正面 / (正面 + 负面)，中性不计入分母；任一侧有效样本不足给"数据不足"。
    返回 {"trend", "recent_rate", "earlier_rate", "recent_n", "earlier_n"}。
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(days=SENTIMENT_WINDOW_DAYS)).strftime("%Y-%m-%d")
    buckets = {"recent": [0, 0], "earlier": [0, 0]}  # [正面, 负面]
    for c in comments:
        t = getattr(c, "time", None) or (c.get("time") if isinstance(c, dict) else None)
        text = getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else "")
        if not t or not text:
            continue
        s = classify_comment(str(text))
        if s == 0:
            continue
        key = "recent" if str(t)[:10] >= cutoff else "earlier"
        buckets[key][0 if s > 0 else 1] += 1
    rn, en = buckets["recent"], buckets["earlier"]
    recent_n, earlier_n = rn[0] + rn[1], en[0] + en[1]
    out = {"trend": "数据不足", "recent_rate": None, "earlier_rate": None,
           "recent_n": recent_n, "earlier_n": earlier_n}
    if recent_n < SENTIMENT_MIN_N or earlier_n < SENTIMENT_MIN_N:
        return out
    recent_rate = rn[0] / recent_n
    earlier_rate = en[0] / earlier_n
    diff = recent_rate - earlier_rate
    if diff >= SENTIMENT_THRESHOLD:
        trend = "好评上升"
    elif diff <= -SENTIMENT_THRESHOLD:
        trend = "好评下降"
    else:
        trend = "口碑平稳"
    out.update({"trend": trend, "recent_rate": round(recent_rate, 2),
                "earlier_rate": round(earlier_rate, 2)})
    return out


def pitfall_digest(all_points: list[dict]) -> list[dict]:
    """避坑专题：汇总避雷要点，高置信度排前，每条带评论原文引用（可为空）。

    排序键：量化置信度评分降序（未标注时按语义标签回退映射），同分再按
    多源一致 > 存分歧 > 单源。行内携带 conf_level/conf_score/n_sources 供渲染展示。"""
    order = {"多源一致": 0, "存分歧": 1, "单源": 2}
    fallback = {"多源一致": CONF_MID, "存分歧": CONF_MID}  # 旧数据无评分时按标签回退
    rows = []
    for p in all_points:
        if p.get("stance") != "避雷":
            continue
        score = p.get("conf_score")
        if score is None:
            score = fallback.get(p.get("confidence", "单源"), CONF_LOW)
        rows.append({
            "claim": p["claim"],
            "quote": p.get("quote") or "",
            "source": p.get("source", ""),
            "confidence": p.get("confidence", "单源"),
            "conf_level": p.get("conf_level", ""),
            "conf_score": score,
            "n_sources": p.get("n_sources", 1),
        })
    rows.sort(key=lambda r: (-r["conf_score"], order.get(r["confidence"], 3)))
    return rows[:DIGEST_MAX]


def time_windows(items: list, now: datetime | None = None) -> dict:
    """发布时间三窗口占比（刷榜趋势判定的原料）：近 7 天 / 7~60 天 / 60 天以上。
    发布时间缺失的视频计入 old60（保守：无法证明新鲜就当旧内容）。纯函数可测。"""
    now = now or datetime.now()
    cut7 = now - timedelta(days=7)
    cut60 = now - timedelta(days=60)
    n = len(items)
    fresh7 = fresh60 = old60 = 0
    for it in items:
        try:
            d = datetime.strptime((it.publish_time or "")[:10], "%Y-%m-%d")
        except ValueError:
            old60 += 1
            continue
        if d >= cut7:
            fresh7 += 1
        elif d >= cut60:
            fresh60 += 1
        else:
            old60 += 1
    return {
        "fresh7": round(fresh7 / n, 2) if n else 0.0,
        "fresh60": round(fresh60 / n, 2) if n else 0.0,
        "old60": round(old60 / n, 2) if n else 0.0,
    }


def trend_of(fresh7: float, old60: float, score: float) -> str:
    """四态趋势判定：本周最火 / 长盛不衰 / 正在降温 / 平稳。纯函数可测。

    注意区分："本周最火"必须看新鲜度（新增内容井喷），综合分高只代表总热度；
    高赞但内容偏旧的景点标"长盛不衰"而非"本周最火"，避免误导。
    """
    if fresh7 >= HOT_FRESH7:
        return "本周最火"
    if old60 >= COOL_OLD60 and fresh7 <= COOL_FRESH7:
        # 无新增内容：高分 = 一直火（长盛不衰），低分 = 真降温
        return "长盛不衰" if score >= HOT_SCORE else "正在降温"
    if score >= HOT_SCORE:
        return "长盛不衰"
    return "平稳"
