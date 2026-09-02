"""多源交叉验证：对提取要点做语义聚类，标注置信度（标签 + 量化评分）。

语义标签三级（报告展示用，兼容旧版）：
- 多源一致：≥2 个独立来源（不同视频）说同一件事；
- 存分歧：来源互相矛盾（如票价说法不一）或立场对立，报告中必须并列呈现；
- 单源：仅一个来源，报告中用"有博主提到"等谨慎语气。

量化置信度三级（conf_level/conf_score/n_sources，营销号来源降级）：
- 高置信度 0.9：≥3 个独立来源且无矛盾；
- 中置信度 0.6：2 个独立来源，或有轻微矛盾（存分歧但多源）；
- 低置信度 0.3：单源，或来源疑似营销号。
独立来源数按"非营销号视频"计（同组内营销号来源不计入印证）。

验证本身用 LLM 调用做语义等价聚类；要点数超过 BATCH_THRESHOLD 时按批次聚类再合并，
避免单次大输入漏分组；任何解析异常都回退为"全部单源"，宁可保守也不阻断主流程。
"""
from config import VERIFY_ENABLE_THINKING
from core.llm import chat_json

# 量化置信度分值（避坑排序/展示用，集中在此便于调参）
CONF_HIGH, CONF_MID, CONF_LOW = 0.9, 0.6, 0.3

# 超过该数量的要点分批聚类：大输入下单次调用容易漏分组，分批后各批编号短小、判断更稳。
BATCH_THRESHOLD = 40
BATCH_SIZE = 30

VERIFY_SYSTEM = """你是信息核查助手。输入是编号的旅游信息要点列表，你的任务：

1. 分组：把"说的是同一件事"的要点归为一组（语义等价即可，允许措辞不同，
   例如两条都说"苏堤不能骑车"）。主体不同、地点不同、行为不同不算同一件事；
2. 冲突：找出互相矛盾的要点，成对列出。两种都算矛盾：
   - 事实矛盾：对同一事物给出不同的价格/开放时间/结论；
   - 立场对立：对同一件事一说"推荐"一说"避雷"；
3. 输出严格 JSON：{"groups": [[编号...], ...], "conflicts": [[编号, 编号], ...]}

要求：groups 必须不重不漏地覆盖全部编号；conflicts 可为空数组；
编号一律使用输入中的原始编号。"""


def _cluster_once(points: list[dict]) -> tuple[list[list[int]], list[tuple[int, int]]]:
    """单次聚类调用：返回 (分组, 冲突对)，编号从 1 开始。解析异常抛给调用方兜底。"""
    n = len(points)
    numbered = "\n".join(
        f"[{i + 1}] ({p['topic']}|立场:{p.get('stance', '中性')}) {p['claim']}"
        for i, p in enumerate(points)
    )
    # 聚类是管道里最依赖推理的环节：开思考更准（实测可修正约 2 成判定）但慢一个量级，
    # 由 VERIFY_ENABLE_THINKING 配置控制，兼顾"快速迭代"与"深度出报告"两种场景
    data = chat_json(VERIFY_SYSTEM, numbered, enable_thinking=VERIFY_ENABLE_THINKING)
    groups = [
        _clean_ids(g, n)
        for g in data.get("groups", [])
        if isinstance(g, list)
    ]
    conflicts = [
        (int(a), int(b))
        for a, b in (data.get("conflicts") or [])
        if _is_id(a, n) and _is_id(b, n) and int(a) != int(b)
    ]
    return groups, conflicts


def _stance_conflicts(points: list[dict], groups: list[list[int]]) -> list[tuple[int, int]]:
    """代码兜底的立场冲突检测：同一分组内既有人说推荐又有人说避雷，
    成对记为冲突——不依赖模型自觉，立场对立绝不漏判。"""
    pairs: list[tuple[int, int]] = []
    for members in groups:
        rec = [x for x in members if points[x - 1].get("stance") == "推荐"]
        avoid = [x for x in members if points[x - 1].get("stance") == "避雷"]
        for a in rec:
            for b in avoid:
                pairs.append((a, b))
    return pairs


def annotate_confidence(points: list[dict],
                        marketing_sources: set[str] | None = None) -> list[dict]:
    """语义聚类 + 置信度标注。marketing_sources 为疑似营销号的视频 URL 集合：
    这些来源不计入独立印证数，且自身要点直接降为低置信度。"""
    n = len(points)
    if n == 0:
        return points
    mkt = marketing_sources or set()

    try:
        if n <= BATCH_THRESHOLD:
            groups, conflicts = _cluster_once(points)
        else:
            # 分批聚类：各批编号独立从 1 起，回填全局编号后合并。
            # 同批内才比较等价关系，跨批漏组会使少量要点降级为单源——保守但安全。
            groups, conflicts = [], []
            for start in range(0, n, BATCH_SIZE):
                batch = points[start:start + BATCH_SIZE]
                b_groups, b_conflicts = _cluster_once(batch)
                groups.append([[x + start for x in g] for g in b_groups])
                conflicts.extend((a + start, b + start) for a, b in b_conflicts)
            groups = [g for sub in groups for g in sub]
    except Exception:
        groups, conflicts = [], []

    # 防御性解析：去重、丢弃越界编号；未被任何组覆盖的编号自成一组
    covered: set[int] = set()
    group_members: list[list[int]] = []
    for g in groups:
        uniq = []
        for x in g:
            if x not in covered:
                covered.add(x)
                uniq.append(x)
        if uniq:
            group_members.append(uniq)
    for i in range(1, n + 1):
        if i not in covered:
            group_members.append([i])

    # 多源的真义是"不同视频的来源数"：同组内去掉重复来源后计数，
    # 同一条视频里的两条相近说法不构成多源印证
    group_of: dict[int, int] = {}
    for gi, members in enumerate(group_members):
        for x in members:
            group_of[x] = gi
    group_sources: list[set[str]] = [set() for _ in group_members]
    for i, p in enumerate(points, 1):
        group_sources[group_of[i]].add(p.get("source", ""))

    conflict_ids = {i for pair in conflicts for i in pair}
    conflict_ids |= {i for pair in _stance_conflicts(points, group_members) for i in pair}

    out = []
    for i, p in enumerate(points, 1):
        q = dict(p)
        sources = group_sources[group_of[i]]
        eff = {s for s in sources if s and s not in mkt}  # 有效独立来源：剔除营销号
        conflicted = i in conflict_ids
        if conflicted:
            q["confidence"] = "存分歧"
        elif len(sources) >= 2:
            q["confidence"] = "多源一致"
        else:
            q["confidence"] = "单源"
        # 量化置信度：高=≥3 独立来源无矛盾；中=2 来源或轻微矛盾；低=单源/营销号来源
        if p.get("source") in mkt and p.get("source"):
            level, score = "低置信度", CONF_LOW  # 来源疑似营销号：无论多少印证都降级
        elif len(eff) >= 3 and not conflicted:
            level, score = "高置信度", CONF_HIGH
        elif len(eff) >= 2:
            level, score = "中置信度", CONF_MID
        else:
            level, score = "低置信度", CONF_LOW
        q["conf_level"] = level
        q["conf_score"] = score
        q["n_sources"] = len(eff)
        out.append(q)
    return out


def _is_id(x, n: int) -> bool:
    try:
        return 1 <= int(str(x).strip()) <= n
    except (ValueError, TypeError):
        return False


def _clean_ids(g: list, n: int) -> list[int]:
    return [int(str(x).strip()) for x in g if _is_id(x, n)]
