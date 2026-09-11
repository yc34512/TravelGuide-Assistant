"""统一数据模型：所有采集结果和 LLM 提取结果都使用这里的结构。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Comment:
    """评论数据。

    合规约束：只保留文本与互动数；评论者的昵称/头像/用户ID 等属于个人信息，
    在采集入口处一律丢弃，不允许进入任何下游存储。
    """

    text: str
    like_count: int | None = None
    is_author_reply: bool = False
    time: str | None = None  # 发布日期 YYYY-MM-DD（由页面相对时间换算；情感趋势用，不含属地等个人信息）


@dataclass
class VideoItem:
    """一条视频的完整采集结果。

    质量指标（play/like/comment/collect/share/duration/publish_time）由采集层从详情接口
    statistics 节点读全（实跑探测已确认这些字段均下发，含 play_count），
    供质量闸筛选与热度计算；旧缓存 JSON 无这些键时为 None，
    质量闸对 None 一律走"不惩罚也不加分"的中性处理（向后兼容）。
    """

    video_id: str
    url: str
    description: str = ""  # 视频文案（含话题标签）
    tags: list[str] = field(default_factory=list)
    play_count: int | None = None      # 播放量：触达广度（易被推荐算法推高，权重低于点赞）
    like_count: int | None = None
    comment_count: int | None = None
    collect_count: int | None = None   # 收藏数：攻略类内容"收藏=真有用"，比点赞更准的质量信号
    share_count: int | None = None
    duration: float | None = None      # 视频时长（秒）；过短的卡点视频信息量低
    publish_time: str | None = None
    transcript: str = ""  # 口播转写文本（ASR 开启时才有；随原始 JSON 持久化，缓存命中可复用）
    play_urls: list[str] = field(default_factory=list)  # 媒体 CDN 临时地址（含纯视频/纯音频流，签名会过期，不持久化）
    comments: list[Comment] = field(default_factory=list)
    # 质量闸产物（M6）：分数供排序，reject 供"绝不静默丢弃"的透明化呈现
    quality_score: float | None = None
    quality_reject: str = ""           # 未过闸原因（低赞/过旧/过短/营销号）；空=通过

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("play_urls", None)  # 临时签名地址不入库
        return d


@dataclass
class InfoPoint:
    """LLM 从单条内容中提取的要点，均带来源 URL，可溯源。"""

    topic: str  # 门票/交通/避雷/打卡/美食/住宿/路线/其他
    claim: str
    source: str
    time_sensitive: bool = False
