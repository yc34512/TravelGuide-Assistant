"""Map 阶段：逐条视频调用 LLM 提取结构化要点（防幻觉约束写死在提示词里）。"""
from core.llm import chat_json
from core.models import VideoItem

EXTRACT_SYSTEM = """你是旅游攻略信息抽取助手，负责从单条抖音内容（视频文案 + 评论区）中抽取旅游相关信息。

规则：
1. 只使用输入文本中出现的信息，禁止用你自己的知识补充或推断；
2. 广告嫌疑内容（推销、导流、团购、探店合作话术）一律忽略；
3. 门票价格、开放时间、预约政策、排队情况类信息必须标记 time_sensitive=true；
4. 每条要点必须带来源 url（就是输入给出的 video_url）；
5. 输出严格 JSON：{"points": [{"topic": "门票|交通|避雷|打卡|美食|住宿|路线|其他", "claim": "一句话要点", "time_sensitive": true/false, "source": "视频url"}]}
没有可抽取的信息时返回 {"points": []}"""


def extract_points(item: VideoItem) -> list[dict]:
    comments_text = "\n".join(
        f"- {c.text}（赞{c.like_count if c.like_count is not None else '?'}）"
        for c in item.comments[:50]
    )
    user = (
        f"video_url: {item.url}\n"
        f"视频文案: {item.description or '(无)'}\n\n"
        f"评论区:\n{comments_text or '(无评论)'}"
    )
    data = chat_json(EXTRACT_SYSTEM, user)

    points: list[dict] = []
    for p in data.get("points", []):
        claim = str(p.get("claim", "")).strip()
        if not claim:
            continue
        points.append(
            {
                "topic": str(p.get("topic", "其他")),
                "claim": claim,
                "time_sensitive": bool(p.get("time_sensitive", False)),
                "source": str(p.get("source") or item.url),
            }
        )
    return points
