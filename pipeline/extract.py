"""Map 阶段：逐条视频调用 LLM 提取结构化要点（防幻觉约束写死在提示词里）。

提示词规则 + 代码过滤双防线：疑问句、标题党、广告导流话术在代码层再兜一次底。
"""
import re

from core.llm import chat_json
from core.models import VideoItem

EXTRACT_SYSTEM = """你是旅游攻略信息抽取助手，负责从单条抖音内容（视频文案 + 评论区）中抽取旅游相关信息。

规则：
1. 只使用输入文本中出现的信息，禁止用你自己的知识补充或推断；
2. 广告嫌疑内容（推销、导流、团购、探店合作话术）一律忽略；
3. 疑问句不是信息：评论里的提问（"是不是要门票""多少钱""怎么走""人多吗"）一律不提取，
   只有陈述性的经验/事实/建议才提取；
4. 视频标题式口号与引导话术没有信息量（"一定要收藏""看这一条就够了""关注我""点赞"），
   不提取；纯情绪感叹（"太美了""绝了"）也不提取，除非它包含具体事实（如"雨天的西湖有雾"）；
5. 门票价格、开放时间、预约政策、排队情况类信息必须标记 time_sensitive=true；
6. 每条要点必须带来源 url（就是输入给出的 video_url）；
7. 输出严格 JSON：{"points": [{"topic": "门票|交通|避雷|打卡|美食|住宿|路线|其他", "claim": "一句话要点", "time_sensitive": true/false, "source": "视频url"}]}
没有可抽取的信息时返回 {"points": []}"""

# 代码层兜底：疑问句（带不带问号都要拦）/ 常见低价值话术
_QUESTION_END_RE = re.compile(r"[??？？]\s*$")
_QUESTION_HINT_RE = re.compile(
    r"是不是|有没有|能不能|可不可以|好不好|要不要|行不行|"
    r"多少钱|多少小时|多少时间|几点|怎么走|怎么去|怎么回|怎么玩|怎么办|哪里|"
    r"有人(知道|告诉|回答)|求(解答|告知|回复)|[么吗]$"
)
_LOWVALUE_RE = re.compile(
    r"一定要收藏|收藏起来|收藏一下|关注(我|一下)|点赞|看这一条就够|评论区见|"
    r"主页|私信|团购|低价|优惠券|代订|探店合作|广告"
)


def _low_value(claim: str) -> bool:
    return bool(
        _QUESTION_END_RE.search(claim)
        or _QUESTION_HINT_RE.search(claim)
        or _LOWVALUE_RE.search(claim)
    )


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
        claim = str(p.get("claim", "")).strip().rstrip("。")
        if not claim or _low_value(claim):
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
