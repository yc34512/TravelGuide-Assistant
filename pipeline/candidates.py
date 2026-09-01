"""混合候选生成与验证筛选（P0 核心）。

链路：大模型圈定 15~20 个候选（景点/美食/体验/购物）→ 逐个抖音验证采集
（由 service.trip 驱动，命中缓存免采）→ 大模型交叉验证筛选出 8~12 个优质候选。

营销号过滤：视频文案命中营销话术正则即被标记，不参与候选评分——
本 P0 数据基线以"文案 + 评论"为主，文案的首要价值就是存在性验证与营销号识别。
"""
import re

from config import LLM_WEB_SEARCH
from core.llm import chat_json

# 营销号文案特征（宁缺勿滥：只命中强营销信号，避免误伤普通探店分享）
MARKETING_RE = re.compile(
    r"团购|推广|合作|探店|点击左下角|优惠券|代金券|限时特惠|找我下单|私我|"
    r"评论区置顶|粉丝福利|商业合作|广告"
)

# 类别配额：景点是行程骨架必须充足，美食/体验点缀，购物仅作预算参考
CATEGORY_QUOTA = {"景点": 10, "美食": 5, "体验": 3, "购物": 2}
TOTAL_CANDIDATES_MAX = 20
VERIFY_MAX = 12          # 验证采集的候选数硬上限（成本控制闸）
KEEP_MIN, KEEP_MAX = 8, 12

GEN_SYSTEM = """你是旅行候选圈定专家。为指定城市生成值得实地验证的候选清单。

规则：
1. 总数 15~20 个，按类别配额：景点约10、美食约5、体验约3、购物约2（购物仅列 1~2 个代表性场所）；
2. 候选要具体到可直接搜索的名称（如"云冈石窟"而非"古建筑"；"凤临阁"而非"好吃的"）；
3. 优先选游客真实会去、有讨论热度的，避免冷门到搜不到内容的；结合近期真实热度与开放情况（如有联网信息可参考）；
4. 结合用户偏好与天数调整侧重（如天数短则砍购物/体验）；
5. 输出严格 JSON：{"candidates": [{"name": "...", "category": "景点|美食|体验|购物", "reason": "一句话理由"}]}"""

VERIFY_SYSTEM = """你是信息核查专家。输入是候选清单及各自的抖音验证统计（正面/负面证据、营销号占比、评论摘录），
判断每个候选是否值得排进行程。

规则：
1. verdict=drop 的情形：完全没有有效视频；正面证据弱且负面证据强；营销号占比超过一半；
2. pitfall_risk 依据负面证据强度：低/中/高；
3. evidence 依据正面证据强度：强/中/弱；
4. reason 一句话说明判断依据（引用关键评论摘录内容）；
5. 最终 keep 的候选控制在 8~12 个：景点类至少保留 4 个（行程骨架），名额不足时优先砍购物、其次体验；
6. 输出严格 JSON：{"results": [{"name": "...", "verdict": "keep|drop", "evidence": "强|中|弱",
   "pitfall_risk": "低|中|高", "reason": "..."}]}"""


def is_marketing(text: str) -> bool:
    """视频文案是否命中营销话术（纯函数，独立可测）。"""
    return bool(MARKETING_RE.search(text or ""))


def _normalize_candidate(x: dict) -> dict | None:
    name = str(x.get("name") or "").strip()
    category = str(x.get("category") or "").strip()
    if not name:
        return None
    if category not in CATEGORY_QUOTA:
        category = "景点"
    return {"name": name, "category": category, "reason": str(x.get("reason") or "").strip()}


def generate_candidates(city: str, days: int, preferences: str) -> list[dict]:
    """大模型圈定候选：按类别配额裁剪后返回（上限 TOTAL_CANDIDATES_MAX）。
    联网搜索默认开启（服务商不支持时自动降级为纯基线）。"""
    data = chat_json(
        GEN_SYSTEM,
        f"城市：{city}\n天数：{days}\n用户偏好：{preferences or '无'}\n"
        f"类别配额：{CATEGORY_QUOTA}",
        web_search=LLM_WEB_SEARCH,
    )
    raw = [_normalize_candidate(x) for x in (data.get("candidates") or []) if isinstance(x, dict)]
    picked: list[dict] = []
    per_cat: dict[str, int] = {c: 0 for c in CATEGORY_QUOTA}
    for c in raw:
        if per_cat[c["category"]] >= CATEGORY_QUOTA[c["category"]]:
            continue
        per_cat[c["category"]] += 1
        picked.append(c)
        if len(picked) >= TOTAL_CANDIDATES_MAX:
            break
    return picked


def _normalize_result(x: dict, known: set[str]) -> dict | None:
    name = str(x.get("name") or "").strip()
    if not name or name not in known:
        return None
    verdict = str(x.get("verdict") or "").strip().lower()
    evidence = str(x.get("evidence") or "").strip()
    risk = str(x.get("pitfall_risk") or "").strip()
    return {
        "name": name,
        "verdict": "keep" if verdict == "keep" else "drop",
        "evidence": evidence if evidence in ("强", "中", "弱") else "弱",
        "pitfall_risk": risk if risk in ("低", "中", "高") else "中",
        "reason": str(x.get("reason") or "").strip(),
    }


def verify_candidates(candidates: list[dict], stats: dict[str, dict]) -> list[dict]:
    """交叉验证筛选：输入候选 + 验证统计，输出带 verdict 的评审结果。

    stats 形如 {name: {"videos", "marketing_hits", "positive", "negative", "sample_quotes"}}。
    兜底：LLM 未覆盖或有缺陷的候选按保守规则补判（有正面证据则 keep）。
    """
    known = {c["name"] for c in candidates}
    lines = []
    for c in candidates:
        s = stats.get(c["name"]) or {}
        ratio = (s.get("marketing_hits") or 0) / max(1, s.get("videos") or 1)
        lines.append(
            f"- {c['name']}（{c['category']}）：视频 {s.get('videos', 0)} 条，"
            f"营销号占比 {ratio:.0%}，正面证据 {s.get('positive', 0)} 条，"
            f"负面证据 {s.get('negative', 0)} 条；评论摘录：{'；'.join((s.get('sample_quotes') or [])[:3]) or '无'}"
        )
    data = chat_json(VERIFY_SYSTEM, "候选验证统计：\n" + "\n".join(lines))
    results = {
        r["name"]: r
        for r in (
            _normalize_result(x, known) for x in (data.get("results") or []) if isinstance(x, dict)
        )
        if r
    }
    # 保守兜底：评审遗漏的候选，有正面证据且营销号不过半则 keep
    for c in candidates:
        if c["name"] in results:
            continue
        s = stats.get(c["name"]) or {}
        ratio = (s.get("marketing_hits") or 0) / max(1, s.get("videos") or 1)
        ok = (s.get("positive") or 0) > 0 and ratio <= 0.5
        results[c["name"]] = {
            "name": c["name"],
            "verdict": "keep" if ok else "drop",
            "evidence": "中" if ok else "弱",
            "pitfall_risk": "中",
            "reason": "自动兜底判定（评审未覆盖）",
        }
    # 数量控制：keep 过多时按证据强度裁到 KEEP_MAX（景点保底不裁）
    cat_of = {c["name"]: c["category"] for c in candidates}
    keeps = [r for r in results.values() if r["verdict"] == "keep"]
    if len(keeps) > KEEP_MAX:
        order = {"强": 0, "中": 1, "弱": 2}
        keeps.sort(key=lambda r: (order.get(r["evidence"], 3), 0 if cat_of[r["name"]] == "景点" else 1))
        cut = {r["name"] for r in keeps[KEEP_MAX:] if cat_of[r["name"]] != "景点"}
        for r in results.values():
            if r["name"] in cut:
                r["verdict"] = "drop"
                r["reason"] += "（超出保留上限被裁剪）"
    return [results[c["name"]] for c in candidates if c["name"] in results]
