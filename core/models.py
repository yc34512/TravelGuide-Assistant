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


@dataclass
class VideoItem:
    """一条视频的完整采集结果。"""

    video_id: str
    url: str
    description: str = ""  # 视频文案（含话题标签）
    tags: list[str] = field(default_factory=list)
    like_count: int | None = None
    comment_count: int | None = None
    publish_time: str | None = None
    comments: list[Comment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InfoPoint:
    """LLM 从单条内容中提取的要点，均带来源 URL，可溯源。"""

    topic: str  # 门票/交通/避雷/打卡/美食/住宿/路线/其他
    claim: str
    source: str
    time_sensitive: bool = False
