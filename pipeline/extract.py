"""Map 阶段：逐条视频调用 LLM 提取结构化要点（防幻觉约束写死在提示词里）。

提示词规则 + 代码过滤双防线：疑问句、标题党、广告导流话术在代码层再兜一次底。
每条要点带 stance 立场（推荐/避雷/中性）：区分内容里"可取"与"不可取"的部分，
供报告生成「值得做/别踩坑」速览清单。
"""
import re
from datetime import datetime

from core.llm import chat_json
from core.models import VideoItem

# 发布超过该年限的视频在提取上下文中标注"较旧"，提醒模型谨慎对待其时效信息；
# 提取阶段拿不到发布时间/点赞数时不参与判断，由下游置信度机制兜底。
STALE_YEARS = 2

EXTRACT_SYSTEM = """你是旅游攻略信息抽取助手，负责从单条抖音内容（视频文案 + 口播转写 + 评论区）中抽取旅游相关信息。

规则：
1. 只使用输入文本中出现的信息，禁止用你自己的知识补充或推断；
2. 广告嫌疑内容（推销、导流、团购、探店合作话术）一律忽略；
3. 疑问句不是信息：评论里的提问（"是不是要门票""多少钱""怎么走""人多吗"）一律不提取，
   只有陈述性的经验/事实/建议才提取；
4. 视频标题式口号与引导话术没有信息量（"一定要收藏""看这一条就够了""关注我""点赞"），
   不提取；纯情绪感叹（"太美了""绝了"）也不提取，除非它包含具体事实（如"雨天的西湖有雾"）；
5. 门票价格、开放时间、预约政策、排队情况类信息必须标记 time_sensitive=true；
6. 每条要点必须带来源 url（就是输入给出的 video_url）；
7. 每条要点必须标注 stance（立场）：
   - "推荐"：内容把这件事说成值得做的（正面经验、好评、强烈推荐、高赞认可的做法）；
   - "避雷"：内容把这件事说成不值得/有坑的（踩坑吐槽、劝退、排队太久、不值、宰客）；
   - "中性"：客观事实/信息陈述，没有明显褒贬（开放时间、交通方式、位置等）；
   立场必须来自原文表述，禁止根据你自己的常识推断；
8. 若输入标注了"发布较旧"，其中的时效敏感信息照提取但照常标注，不要擅自丢弃；
9. 输出严格 JSON：{"points": [{"topic": "门票|交通|避雷|打卡|美食|住宿|路线|其他", "claim": "一句话要点", "stance": "推荐|避雷|中性", "time_sensitive": true/false, "source": "视频url"}]}
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


_ALLOWED_STANCES = {"推荐", "避雷", "中性"}


def _is_stale(publish_time: str | None) -> bool:
    """发布超过 STALE_YEARS 年的视频视为较旧：时效信息可能已失效。"""
    if not publish_time:
        return False
    try:
        pub = datetime.strptime(publish_time[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return (datetime.now() - pub).days > STALE_YEARS * 365


def extract_points(item: VideoItem) -> list[dict]:
    comments_text = "\n".join(
        f"- {c.text}（赞{c.like_count if c.like_count is not None else '?'}）"
        for c in item.comments[:50]
    )
    like_str = f"{item.like_count}" if item.like_count is not None else "未知"
    meta = f"video_url: {item.url}\n发布时间: {item.publish_time or '未知'}（视频点赞 {like_str}）"
    if _is_stale(item.publish_time):
        meta += "  ← 发布较旧（超过两年），其中价格/政策类信息可能已变化"
    user = (
        f"{meta}\n"
        f"视频文案: {item.description or '(无)'}\n"
        f"视频口播内容: {item.transcript or '(无)'}\n\n"
        f"评论区:\n{comments_text or '(无评论)'}"
    )
    data = chat_json(EXTRACT_SYSTEM, user)

    points: list[dict] = []
    for p in data.get("points", []):
        claim = str(p.get("claim", "")).strip().rstrip("。")
        if not claim or _low_value(claim):
            continue
        stance = str(p.get("stance", "中性")).strip()
        points.append(
            {
                "topic": str(p.get("topic", "其他")),
                "claim": claim,
                "stance": stance if stance in _ALLOWED_STANCES else "中性",
                "time_sensitive": bool(p.get("time_sensitive", False)),
                "source": str(p.get("source") or item.url),
            }
        )
    return points
