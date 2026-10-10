"""Embedding 客户端：走已有 LLM 服务商的 OpenAI 兼容 /embeddings 端点。

设计依据（实现规格 §12.5 / §13.5）：
- 不引入 torch 等本地推理依赖——embedding 走项目已在的百炼/通义生态；
- §12（点位名实体判定）与 §13（文本检索）**共用本组件**，但各自分开建索引
  （"entity" / "corpus" 两个 collection，见 core/vector_store.py），不得混用；
- 降级链（§12.6 / §13.6）：无 Key / 无网络 / 端点不支持 embedding 时，
  本模块返回 None，由调用方跳过向量路或退回压缩档案路径——**绝不抛异常中断主流程**。

"失败一次不再重试"：端点是模型级能力，不支持就是全进程都不支持；
每次都等一次超时会拖慢每个候选。探测失败后打标（_PROBE_FAILED），后续直接返回 None。

离线测试铁律：tests_offline.py 的"不联网、不调 LLM"不得被破坏——
测试通过 set_embedder() 注入固定向量的假实现（或 reset_embedder() 后不打标，
让 ensure/available 走"无 Key"分支）。
"""
from __future__ import annotations

import threading

from config import EMBEDDING_MODEL

_LOCK = threading.Lock()
_INJECTED = None            # 测试注入的 embedder：fn(texts) -> list[list[float]]
_PROBE_FAILED = False       # 端点探测失败标记：本进程不再反复尝试
_CACHE: dict[str, list[float]] = {}   # 进程级缓存：同一文本不重复请求
_CACHE_MAX = 4000
# 批量上限：单次请求条数过大时部分服务商会直接拒绝（宁可分批，不要整体失败）
_BATCH = 16


def set_embedder(fn) -> None:
    """注入自定义 embedder（离线测试 / 特殊环境）：fn(texts: list[str]) -> list[list[float]]。"""
    global _INJECTED
    with _LOCK:
        _INJECTED = fn


def reset_embedder() -> None:
    """还原为真实端点并清空缓存与失败标记（测试 teardown / 长跑进程重启探测）。"""
    global _INJECTED, _PROBE_FAILED
    with _LOCK:
        _INJECTED = None
        _PROBE_FAILED = False
        _CACHE.clear()


def _endpoint_embed(texts: list[str]) -> list[list[float]]:
    """真实端点调用（任何异常向上抛，由 embed_texts 统一兜底为 None）。

    分批调用：单请求条数超限被拒时不会整体失败。返回顺序与输入严格一致
    （按响应条目的 index 排序，部分服务商不保证顺序）。"""
    from core.credentials import get_llm_config
    from openai import OpenAI

    cfg = get_llm_config()
    if not cfg:
        raise RuntimeError("未配置 API Key")
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg.get("base_url"), timeout=60)
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        chunk = texts[i:i + _BATCH]
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=chunk)
        items = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        out.extend(list(d.embedding) for d in items)
    return out


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """批量 embed。返回与输入等长的向量列表；任何失败返回 None（静默降级）。

    命中进程缓存的文本不重复请求；注入的 mock 不受 _PROBE_FAILED 影响
    （测试可先造"端点失败"再注入，验证降级路径不串味）。"""
    global _PROBE_FAILED
    items = [str(t or "").strip() for t in (texts or [])]
    if not items:
        return []
    if any(not t for t in items):
        return None   # 空文本不作为可检索对象，整体拒绝（调用方应先过滤）
    with _LOCK:
        fn = _INJECTED
        if fn is None and _PROBE_FAILED:
            return None
        todo = [t for t in dict.fromkeys(items) if t not in _CACHE]
    if todo:
        fn = fn or _endpoint_embed
        try:
            vecs = fn(todo)
        except Exception:
            if _INJECTED is None:
                with _LOCK:
                    _PROBE_FAILED = True
            return None
        if not vecs or len(vecs) != len(todo):
            return None
        if any(not isinstance(v, (list, tuple)) or not v for v in vecs):
            return None
        with _LOCK:
            if len(_CACHE) + len(todo) > _CACHE_MAX:   # 防意外增长：满了先清（本地规模无压力）
                _CACHE.clear()
            for t, v in zip(todo, vecs):
                _CACHE[t] = [float(x) for x in v]
    with _LOCK:
        out = [_CACHE.get(t) for t in items]
    if any(v is None for v in out):
        return None
    return [list(v) for v in out]


def available() -> bool:
    """向量路是否可能可用（不发请求）：注入的 mock 恒可用；
    真实端点看"有 Key 且未标记失败"。真实可用性以 embed_texts 的返回为准。"""
    with _LOCK:
        if _INJECTED is not None:
            return True
        if _PROBE_FAILED:
            return False
    try:
        from core.credentials import get_llm_config
        return bool(get_llm_config())
    except Exception:
        return False
