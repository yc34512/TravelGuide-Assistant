"""候选四层漏斗的纯逻辑（设计规格 §4.3 ~ §4.5）：探测排序 / 达标判定 / 回补名单。

为什么单独成模块：这些判定必须能**离线断言**（§7.3）——不依赖网络、LLM、浏览器，
用夹具即可验证；service.trip 只负责把数据喂进来并执行动作（生成与裁判分离，§7.4）。

分层对应（§3 总览）：
- ② 复用层：core.entity_match.resolve_prior_records（不在此模块）；
- ③ 探测层：条数预筛的**判定**在此（排序 + 淘汰），实际浏览器采集在
  service.research.probe_result_counts（一次会话批量完成，D16）；
- ④ 采集层：深采排序（§4.4）+ 达标判定（D7 多条件 / D8 便宜规则）；
- ⑤ 回补层：排除清单（D11）+ 状态汇总（可观测，绝不静默）。
"""
from __future__ import annotations

from collections import Counter

from config import COLLECT_KEEP_SPOTS_MIN, PROBE_MIN_RESULTS
from core import knowledge
from pipeline.candidates import KEEP_MIN   # 总 keep 口径单一来源（与交叉验证 KEEP_MIN 同源）

# 类别权重（§4.4："与 VERIFY_QUOTA 同口径"—— 景点 > 美食 > 体验 > 购物）
CATEGORY_ORDER = {"景点": 0, "美食": 1, "体验": 2, "购物": 3}
_UNKNOWN_CAT_ORDER = 9


def rank_collect_queue(cands: list[dict], draft_names: list[str] | None = None,
                       probe_counts: dict[str, int] | None = None) -> list[dict]:
    """深采排序（§4.4）：草案点位 > 探测条数高 > 其余按类别权重。

    - 草案点位最优先且保持草案内原顺序（行程主干，必须逐个验证）；
    - 有探测条数的排在未探测的之前（"有证据先采"，未探测不代表"没内容"，不淘汰）；
    - 有条数组内：条数降序，同条数按类别权重；未探测组内：直接按类别权重
      （景点 > 美食 > 体验 > 购物，与 VERIFY_QUOTA 同口径）；
    - 完全同序时保持原顺序（稳定排序）。
    纯函数，独立可测。"""
    drafts: dict[str, int] = {}
    for i, x in enumerate(draft_names or []):
        n = str(x or "").strip()
        if n and n not in drafts:
            drafts[n] = i
    counts = probe_counts or {}

    def _key(c: dict):
        name = str(c.get("name") or "").strip()
        if name in drafts:
            return (0, drafts[name])
        cat = CATEGORY_ORDER.get(str(c.get("category") or "").strip(), _UNKNOWN_CAT_ORDER)
        cnt = counts.get(name)
        if isinstance(cnt, (int, float)):
            return (1, 0, -int(cnt), cat)
        return (1, 1, cat, 0)

    return sorted(cands or [], key=_key)


def probe_drop(name: str, count: int | None, *, min_results: int | None = None,
               user_specified: bool = False) -> bool:
    """探测淘汰判定（§4.3 规则 2）。

    - 用户显式指定的点位**不可淘汰**（D6：用户说了要去，不能因为"搜不到"就丢掉）——
      只做预警，由调用方显式反馈；
    - 未探测/探测失败（count=None）不淘汰——没有证据不淘汰（保守）；
    - 只有"探测到了条数且低于阈值"才判 dropped_probe。

    纯函数，独立可测。"""
    if user_specified:
        return False
    if count is None:
        return False
    thr = PROBE_MIN_RESULTS if min_results is None else int(min_results)
    return int(count) < max(0, thr)


def order_names_by_probe(names: list[str], probe_counts: dict[str, int | None] | None) -> list[str]:
    """按探测条数降序排列名字（用户指定路径的"先采谁"排序，§6：排序生效、淘汰不生效）。

    未探测（None）排在有结果之后——没有证据不算"差"，只是让有证据的先采；
    条数相同保持原顺序（稳定排序，用户原始顺序即 tiebreak）。纯函数，独立可测。"""
    counts = probe_counts or {}

    def _key(n: str):
        c = counts.get(n)
        return (-int(c),) if isinstance(c, (int, float)) else (1,)

    return sorted([str(n or "").strip() for n in (names or []) if str(n or "").strip()],
                  key=_key)


def cheap_keep(stat: dict) -> bool:
    """便宜规则判定"这个候选算一个 keep"（D8）：
    有正面证据（positive > 0）且营销号占比 ≤ 0.5 —— 与 verify_candidates 的保守兜底同口径。

    为什么必须便宜：达标判定发生在**每采完一个候选后**，若每次都调 LLM 完整交叉验证，
    成本会超过省下的采集（D8）。到线后只触发一次完整的 verify_candidates 确认。"""
    pos = 0
    try:
        pos = int(stat.get("positive") or 0)
    except (TypeError, ValueError):
        pos = 0
    vids = max(1, int(stat.get("videos") or 0))
    try:
        ratio = float(stat.get("marketing_hits") or 0) / vids
    except (TypeError, ValueError):
        ratio = 1.0
    return pos > 0 and ratio <= 0.5


def evaluate_target(keeps_by_cat: dict[str, int], keep_min: int | None = None,
                    spots_min: int | None = None) -> dict:
    """达标判定（D7：多条件，缺一不可）。

    达标 ⟺ 总 keep ≥ KEEP_MIN(8) 且 景点类 keep ≥ 4（行程骨架）且 美食类 keep > 0（午晚餐刚需）。

    返回 {"met", "total", "spots", "foods", "missing"}；missing 是缺口说明（进日志/已知妥协，
    绝不明说"感觉不够"）。纯函数，独立可测。"""
    km = KEEP_MIN if keep_min is None else int(keep_min)
    sm = COLLECT_KEEP_SPOTS_MIN if spots_min is None else int(spots_min)
    kc = keeps_by_cat or {}
    total = sum(int(v or 0) for v in kc.values())
    spots = int(kc.get("景点") or 0)
    foods = int(kc.get("美食") or 0)
    missing: list[str] = []
    if total < km:
        missing.append(f"总数 {total}/{km}")
    if spots < sm:
        missing.append(f"景点 {spots}/{sm}")
    if foods <= 0:
        missing.append("美食 0（需 ≥1）")
    return {"met": not missing, "total": total, "spots": spots, "foods": foods,
            "missing": "；".join(missing)}


# —— 回补层（§4.5 / D11 / D12）——

# 已处理过的名单（情况 B 的排除清单，D11 原文：crawled_ok / crawled_empty / dropped_probe）
_EXCLUDE_STATUSES = (knowledge.ST_CRAWLED_OK, knowledge.ST_CRAWLED_EMPTY,
                     knowledge.ST_DROPPED_PROBE)


def fillback_exclusions(records: dict[str, dict]) -> list[str]:
    """情况 B 回补的排除清单：已处理过的候选名（明确要求生成层"这些不要再给"）。

    不含 reused（它是"可复用"不是"已排除"）与 skipped_budget（它还要续采）——
    与状态表（§4.4）的语义严格一致。纯函数，独立可测。"""
    return [k for k, v in (records or {}).items()
            if str((v or {}).get("status") or "") in _EXCLUDE_STATUSES]


def pending_resume(records: dict[str, dict]) -> list[str]:
    """情况 A 续采名单：标了 skipped_budget 的候选（下次必须采，不重新生成候选）。"""
    return [k for k, v in (records or {}).items()
            if str((v or {}).get("status") or "") == knowledge.ST_SKIPPED_BUDGET]


def is_all_processed(records: dict[str, dict], names: list[str]) -> bool:
    """队列是否"全部处理完"（都有终态）——情况 A（还有没采的）/ 情况 B（都处理完了）的分界。"""
    rec = records or {}
    for n in names or []:
        st = str((rec.get(n) or {}).get("status") or "")
        if st in ("", knowledge.ST_SKIPPED_BUDGET):
            return False
    return True


def status_summary(records: dict[str, dict]) -> str:
    """把候选处理档案压成一句人话（进日志：淘汰/跳过/降级都要写清原因与数量，绝不静默）。"""
    cnt = Counter(str((v or {}).get("status") or "?") for v in (records or {}).values())
    parts = [f"{knowledge.STATUS_LABELS.get(k, k)} {n}" for k, n in
             sorted(cnt.items(), key=lambda x: -x[1]) if n]
    return "、".join(parts) if parts else "无"
