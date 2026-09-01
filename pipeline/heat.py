"""热度分析与避坑专题（P0）。

热度指数三因子（全部来自结构化采集数据，不依赖视频内容）：
- 互动热度：视频点赞总量（log 归一，防头部碾压）；
- 评论密度：评论数 / 视频数（讨论活跃度的代理指标）；
- 新鲜度：近 90 天发布视频的占比（"最近突然火了"的信号）。

避坑专题：避雷立场要点汇总，每条附评论原文引用 + 来源 + 置信度，多源一致排前。
两个函数都是纯函数，独立可测。
"""
import math
from datetime import datetime, timedelta

FRESH_DAYS = 90
DIGEST_MAX = 12  # 避坑专题条数上限，多了没人看

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

    返回 {"score": 0~1, "trend": "近期热度上升"|"平稳", "videos", "likes", "comments", "fresh_ratio"}。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=FRESH_DAYS)
    n = len(items)
    likes = sum(it.like_count or 0 for it in items)
    comments = sum(len(it.comments) for it in items)
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
    }


def pitfall_digest(all_points: list[dict]) -> list[dict]:
    """避坑专题：汇总避雷要点，多源一致排前，每条带评论原文引用（可为空）。"""
    order = {"多源一致": 0, "存分歧": 1, "单源": 2}
    rows = [
        {
            "claim": p["claim"],
            "quote": p.get("quote") or "",
            "source": p.get("source", ""),
            "confidence": p.get("confidence", "单源"),
        }
        for p in all_points
        if p.get("stance") == "避雷"
    ]
    rows.sort(key=lambda r: order.get(r["confidence"], 3))
    return rows[:DIGEST_MAX]
