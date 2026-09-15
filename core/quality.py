"""采集质量闸：平台中立的视频质量评估，只吃 core.models.VideoItem。

为什么必须"先筛后采"：抖音是会话级风控——连续 5~6 次视频页导航后弹 3D 验证码中间页，
之后整个会话返回 0 结果。所以"多采几倍详情页再挑好的"不可行（会把任务直接采废）。
本模块的门槛判定同时服务两处，成本从低到高：
1. 搜索页候选池预筛（screen_pool）：一次导航 + 多轮滚动收满候选池，滚动不产生新域名
   请求，风控成本≈0；能解析到卡片点赞时，筛选在这里就完成，详情页导航次数与从前相同。
2. 详情页二次校验（gate_item）：搜索页信息不足时逐条兜底，配合候补队列顶上，
   总导航次数受 config.MAX_DETAIL_FETCH 硬上限约束。

三档门槛 strict/normal/loose，达标数不足自动降档（relax_level），降档轨迹由调用方
写进日志与报告——绝不静默放宽标准。

指标口径（已实跑探测确认）：详情接口 statistics 下发
['admire_count','aweme_id','collect_count','comment_count','digg_count','play_count',
 'recommend_count','share_count']，节点级还有 duration/create_time/aweme_type。
质量分取其中语义明确的五项：播放量（触达广度）+ 点赞 + 收藏 + 评论数 + 新鲜度；
收藏对攻略类内容尤其准（用户觉得有用才收藏）。admire_count/recommend_count 语义不确定，
不纳入（宁缺勿滥）。任一指标缺失按"不加分"保守处理，不猜数。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from config import (MIN_KEEP_BEFORE_RELAX, QUALITY_LEVEL, QUALITY_LEVELS,
                    QUALITY_RELAX_ORDER)

# 营销号文案特征（宁缺勿滥：只命中强营销信号，避免误伤普通探店分享）。
# 口径唯一：候选筛选、置信度降级、热度榜营销号占比、质量闸共用这一份正则。
MARKETING_RE = re.compile(
    r"团购|推广|合作|探店|点击左下角|优惠券|代金券|限时特惠|找我下单|私我|"
    r"评论区置顶|粉丝福利|商业合作|广告"
)

# —— 质量分权重（可复算，报告展示各维度贡献，不搞黑箱排序）——
# 播放量反映触达广度但易被推荐算法推高，所以权重低于点赞；
# 收藏是攻略类内容最硬的"真有用"信号，权重与点赞量级相近。
WEIGHTS = {"play": 0.20, "likes": 0.35, "collect": 0.20, "comments": 0.10, "fresh": 0.15}
# 对数归一锚点：达到该量级即视为满分（抖音头部攻略视频的实际量级）
ANCHORS = {"play": 1_000_000, "likes": 100_000, "collect": 20_000, "comments": 5_000}
MARKETING_PENALTY = 0.5       # 文案命中营销话术：质量分砍半（刷量会推高点赞，不能只看数）
FRESH_DAYS_FULL = 30          # 30 天内发布：新鲜度满分
FRESH_DAYS_ZERO = 730         # 超过 730 天：新鲜度 0 分（两点之间线性衰减）
UNKNOWN_NEUTRAL = 0.5         # 发布时间未知时的新鲜度中性分（不奖不罚，旧缓存兼容）

# 拒绝原因（进日志与报告，用户看得见筛掉了什么）
REJECT_LIKES = "点赞不足"
REJECT_OLD = "内容过旧"
REJECT_SHORT = "时长过短"
REJECT_MARKETING = "营销号文案"


def is_marketing(text: str) -> bool:
    """视频文案是否命中营销话术（纯函数，独立可测）。"""
    return bool(MARKETING_RE.search(text or ""))


def thresholds(level: str | None = None) -> dict:
    """取某档门槛（未知档位退回配置默认档）。返回副本，调用方改不坏全局配置。"""
    lv = level if level in QUALITY_LEVELS else QUALITY_LEVEL
    return dict(QUALITY_LEVELS[lv])


def relax_level(level: str) -> str | None:
    """降一档；已是最后一名（loose）返回 None——再不够也只能如实标注素材不足。"""
    order = list(QUALITY_RELAX_ORDER)
    if level not in order:
        return order[-1] if order else None
    i = order.index(level)
    return order[i + 1] if i + 1 < len(order) else None


def video_age_days(publish_time: str | None, now: datetime | None = None) -> float | None:
    """发布距今天数；日期缺失或非法返回 None（门槛对 None 不生效，不猜数）。"""
    if not publish_time:
        return None
    now = now or datetime.now()
    try:
        d = datetime.strptime(str(publish_time)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return max(0.0, (now - d).total_seconds() / 86400.0)


def _log_norm(value, anchor: int) -> float:
    """对数归一到 0~1；None/非法值按 0 计（未知指标不加分，保守）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    return min(1.0, math.log1p(v) / math.log1p(max(1, anchor)))


def fresh_score(publish_time: str | None, now: datetime | None = None) -> float:
    """新鲜度分 0~1：30 天内满分，730 天以上 0 分，中间线性衰减；日期未知给中性分。"""
    age = video_age_days(publish_time, now)
    if age is None:
        return UNKNOWN_NEUTRAL
    if age <= FRESH_DAYS_FULL:
        return 1.0
    if age >= FRESH_DAYS_ZERO:
        return 0.0
    return 1.0 - (age - FRESH_DAYS_FULL) / (FRESH_DAYS_ZERO - FRESH_DAYS_FULL)


def quality_breakdown(item, now: datetime | None = None) -> dict:
    """质量分各维度明细（纯函数，可离线断言）。返回 {维度: 归一分} 与加权总分。

    item 可以是 VideoItem，也可以是搜索页候选 dict（只有 like_count 时其余维度按 0 计）。"""
    get = (item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d))
    play = _log_norm(get("play_count"), ANCHORS["play"])
    likes = _log_norm(get("like_count"), ANCHORS["likes"])
    collect = _log_norm(get("collect_count"), ANCHORS["collect"])
    comments = _log_norm(get("comment_count") if get("comment_count") is not None
                         else (len(get("comments") or []) if not isinstance(item, dict) else None),
                         ANCHORS["comments"])
    fresh = fresh_score(get("publish_time"), now)
    parts = {"play": play, "likes": likes, "collect": collect,
             "comments": comments, "fresh": fresh}
    score = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)
    if is_marketing(str(get("description") or "")):
        score *= (1 - MARKETING_PENALTY)
        parts["marketing"] = True
    else:
        parts["marketing"] = False
    parts["score"] = round(score, 4)
    return parts


def video_quality_score(item, now: datetime | None = None) -> float:
    """视频质量分 0~1（越高越值得采）。纯函数，独立可测。"""
    return quality_breakdown(item, now)["score"]


def reject_reason(item, level: str | None = None, now: datetime | None = None) -> str:
    """详情页二次校验：返回未过闸原因（空串=通过）。纯函数，独立可测。

    判定顺序按"最硬的先说"：营销号 > 点赞 > 时效 > 时长。
    指标缺失（None）时该维度不生效——旧缓存数据与接口改版都不会被误杀。"""
    th = thresholds(level)
    now = now or datetime.now()
    get = (item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d))
    if is_marketing(str(get("description") or "")):
        return REJECT_MARKETING
    like = get("like_count")
    if isinstance(like, int) and th["min_likes"] and like < th["min_likes"]:
        return REJECT_LIKES
    age = video_age_days(get("publish_time"), now)
    if age is not None and th["max_age_days"] and age > th["max_age_days"]:
        return REJECT_OLD
    dur = get("duration")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    if dur is not None and th["min_duration"] and dur < th["min_duration"]:
        return REJECT_SHORT
    return ""


def passes_gate(item, level: str | None = None, now: datetime | None = None) -> bool:
    """是否通过该档质量闸。"""
    return not reject_reason(item, level, now)


def screen_pool(pool: list[dict], target_n: int, level: str | None = None,
                min_keep: int | None = None, now: datetime | None = None) -> dict:
    """搜索页候选池预筛（成本最低的一道闸）：质量分排序 + 门槛过滤 + 不足自动降档。

    pool 为搜索页候选字典列表（至少含 url/video_id，可能含 like_count）；
    target_n 为首批要深采的条数。返回：
      kept      全部达标候选（已按质量分降序，每条附 quality_score）——前 target_n 首批
                深采，其余作候补队列（详情页校验不达标时顶上，不用重搜）
      picked    kept 的前 target_n 条（首批深采名单）
      level     实际生效档位；"empty" = 搜索无结果（不是门槛问题）、
                "deferred" = 搜索页无点赞数据，校验延后到详情页
      relaxed   降档轨迹（如 ["strict→normal"]），供日志与报告如实标注
      reasons   {淘汰原因: 条数}，按最终档位统计（全链路统一用 reasons 表示统计字典，
                rejected 只用于表示被淘汰的对象列表，同名同义不得分裂）
      pool_size 候选池总数
    候选池无点赞信息时（选择器失效）门槛无法在搜索页判定，level 标为 "deferred"
    ——交给详情页 gate_item 逐条兜底，绝不在这里瞎猜。"""
    lv = level if level in QUALITY_LEVELS else QUALITY_LEVEL
    min_keep = min_keep or MIN_KEEP_BEFORE_RELAX
    pool = list(pool or [])
    # 候选池为空：这是"搜索没结果"（撞验证码风控 / 关键词过冷 / 选择器失效），
    # 不是"门槛太严"。绝不能走降档链并报成"素材不足已降档"，否则把排查方向带偏
    if not pool:
        return {"kept": [], "picked": [], "level": "empty", "relaxed": [],
                "reasons": {}, "pool_size": 0}
    scored = []
    for c in pool:
        cc = dict(c)
        cc["quality_score"] = video_quality_score(cc, now)
        scored.append(cc)
    # 无一条能解析到点赞：搜索页筛不了，整体延后到详情页校验（不猜、不静默降标准）
    if scored and all(c.get("like_count") in (None, 0) for c in scored):
        return {"kept": scored, "picked": scored[:max(1, target_n)], "level": "deferred",
                "relaxed": [], "reasons": {}, "pool_size": len(pool)}
    scored.sort(key=lambda c: c["quality_score"], reverse=True)
    relaxed: list[str] = []
    while True:
        kept, reasons = [], {}
        for c in scored:
            why = reject_reason(c, lv, now)
            if why:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            kept.append(c)   # 达标的全留：首批只深采 target_n 条，其余当候补
        if len(kept) >= min(min_keep, target_n) or lv == QUALITY_RELAX_ORDER[-1]:
            return {"kept": kept, "picked": kept[:max(1, target_n)], "level": lv,
                    "relaxed": relaxed, "reasons": reasons, "pool_size": len(pool)}
        nxt = relax_level(lv)
        relaxed.append(f"{lv}→{nxt}")
        lv = nxt


def gate_and_backfill(candidates: list[dict], items: list, target_n: int,
                      level: str | None = None, now: datetime | None = None) -> dict:
    """详情页校验 + 候补统计（供编排层写日志）：把已深采的 items 分成过闸/未过闸两堆。

    返回 {kept, rejected, reasons, need_more, level}：rejected 是被淘汰的条目列表、
    reasons 是 {原因: 条数} 统计。与 crawler.tabs.fetch_videos_gated 的返回键同名同义
    （同一事实只能有一套口径，不得一处 rejected 指字典、另一处指列表）。
    本函数不做采集（采集在 crawler.tabs，受 MAX_DETAIL_FETCH 硬上限约束），只做判定，
    因此纯函数、可离线断言。"""
    lv = level if level in QUALITY_LEVELS else QUALITY_LEVEL
    kept, rejected_items, reasons = [], [], {}
    for it in items or []:
        why = reject_reason(it, lv, now)
        if why:
            try:
                it.quality_reject = why
                it.quality_score = video_quality_score(it, now)
            except Exception:
                pass
            reasons[why] = reasons.get(why, 0) + 1
            rejected_items.append(it)
            continue
        try:
            it.quality_reject = ""
            it.quality_score = video_quality_score(it, now)
        except Exception:
            pass
        kept.append(it)
    kept.sort(key=lambda x: (getattr(x, "quality_score", None) or 0.0), reverse=True)
    return {"kept": kept, "rejected": rejected_items, "reasons": reasons,
            "need_more": max(0, target_n - len(kept)), "level": lv}


def filter_note(result: dict, target_n: int) -> str:
    """把筛选结果写成一句人话（日志与报告共用，绝不静默丢弃）。

    result 为 screen_pool 或 gate_and_backfill 的返回值（两者 reasons 同名同义）。"""
    pool_size = result.get("pool_size")
    kept = result.get("picked") or result.get("kept") or []
    n_all = len(result.get("kept") or [])
    reasons = result.get("reasons") or {}
    relaxed = result.get("relaxed") or []
    bits = []
    if pool_size:
        bits.append(f"候选池 {pool_size} 条")
    bits.append(f"达标 {min(len(kept), target_n)}/{target_n} 条"
                + (f"（候补 {n_all - target_n} 条）" if n_all > target_n else ""))
    if reasons:
        bits.append("淘汰 " + "、".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    lv = result.get("level")
    if lv == "empty":
        bits.append("搜索无结果（验证码风控/关键词过冷/选择器失效），非门槛过严、未发生降档")
    elif lv == "deferred":
        bits.append("搜索页无点赞数据，质量校验延后到详情页逐条判定")
    elif lv:
        bits.append(f"门槛档 {lv}")
    if relaxed:
        bits.append(f"素材不足已降档（{'，'.join(relaxed)}）——如实标注，未静默放宽")
    likes = [getattr(k, "like_count", None) if not isinstance(k, dict) else k.get("like_count")
             for k in kept]
    likes = [x for x in likes if isinstance(x, int) and x > 0]
    if likes:
        bits.append(f"点赞区间 {min(likes)}~{max(likes)}")
    return "；".join(bits)
