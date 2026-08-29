"""多源交叉验证：对提取要点做语义聚类，标注置信度。

置信度三级：
- 多源一致：≥2 个独立来源（不同视频）说同一件事，可信度最高；
- 存分歧：来源互相矛盾（如票价说法不一），报告中必须并列呈现；
- 单源：仅一个来源，报告中用"有博主提到"等谨慎语气。

验证本身用一次 LLM 调用做语义等价聚类；任何解析异常都回退为"全部单源"，
宁可保守也不阻断主流程。
"""
from config import VERIFY_ENABLE_THINKING
from core.llm import chat_json

VERIFY_SYSTEM = """你是信息核查助手。输入是编号的旅游信息要点列表，你的任务：

1. 分组：把"说的是同一件事"的要点归为一组（语义等价即可，允许措辞不同，
   例如两条都说"苏堤不能骑车"）。主体不同、地点不同、行为不同不算同一件事；
2. 冲突：找出互相矛盾的要点（例如对同一事物给出不同的价格/开放结论），成对列出；
3. 输出严格 JSON：{"groups": [[编号...], ...], "conflicts": [[编号, 编号], ...]}

要求：groups 必须不重不漏地覆盖全部编号；conflicts 可为空数组；
编号一律使用输入中的原始编号。"""


def annotate_confidence(points: list[dict]) -> list[dict]:
    n = len(points)
    if n == 0:
        return points

    numbered = "\n".join(
        f"[{i + 1}] ({p['topic']}) {p['claim']}" for i, p in enumerate(points)
    )
    try:
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

    out = []
    for i, p in enumerate(points, 1):
        q = dict(p)
        if i in conflict_ids:
            q["confidence"] = "存分歧"
        elif len(group_sources[group_of[i]]) >= 2:
            q["confidence"] = "多源一致"
        else:
            q["confidence"] = "单源"
        out.append(q)
    return out


def _is_id(x, n: int) -> bool:
    try:
        return 1 <= int(str(x).strip()) <= n
    except (ValueError, TypeError):
        return False


def _clean_ids(g: list, n: int) -> list[int]:
    return [int(str(x).strip()) for x in g if _is_id(x, n)]
