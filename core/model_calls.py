"""规划范围的显式模型调用：持久预算、响应复用及唯一重试层。"""
from __future__ import annotations

import json
import time

from core.planning_runs import BudgetExhausted, RunError, fingerprint


class FatalModelError(RunError):
    pass


def request_json(store, rid, node, call_id, system, user, *, client_factory=None,
                 retries=3, sleep=None):
    from core import llm
    sleep = sleep or time.sleep
    ih = fingerprint({"system": system, "user": user, "prompt_version": "plan-v1"})
    store.guard(rid)
    saved = store.response(rid, call_id, ih)
    if saved is not None:
        store.event(rid, node, "response_reused", call_id)
        return saved
    row = store.get(rid)
    if row["used_attempts"] >= row["max_attempts"]:
        raise BudgetExhausted("规划实际请求预算耗尽")
    factory = client_factory or llm.planning_client_and_model
    client, model = factory()
    # 显式关闭SDK内置重试：一次create对应一个HTTP发送尝试。
    client = client.with_options(max_retries=0, timeout=120)
    try:
        return _with_client(store, rid, node, call_id, ih, system, user, client, model, retries, sleep)
    finally:
        if client_factory is None:
            client.close()


def _with_client(store, rid, node, call_id, ih, system, user, client, model, retries, sleep):
    from core import llm
    cfg = {"model": model, "endpoint": str(client.base_url)}
    bound = store.step(rid, "model_config")
    if bound is None:
        store.put_step(rid, "model_config", cfg)
    elif bound != cfg:
        raise FatalModelError("模型或接入点已变更，请新建run，不能混用比较条件")
    last = None
    for _ in range(retries):
        store.guard(rid)
        eid = store.reserve(rid, node, call_id, ih, model, retry_limit=retries)
        usage = None
        try:
            response = client.chat.completions.create(
                model=model, temperature=0.2, response_format={"type": "json_object"},
                extra_body=llm._extra_body(False, False),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            usage = getattr(response, "usage", None)
            payload = json.loads(response.choices[0].message.content)
            if not isinstance(payload, dict):
                raise ValueError("模型JSON必须是对象")
            # 响应先进入独立账本；图检查点尚未写入时也可复用已保存的响应。
            store.settle(eid, status="success", usage=usage, response=payload,
                         rid=rid, call_id=call_id, input_hash=ih)
            return payload
        except Exception as exc:
            # 不记录服务商异常正文：它可能回显header、Key或整份用户材料。
            status_code = getattr(exc, "status_code", None)
            fatal = (llm._is_account_fatal(exc) or status_code in {400, 401, 403, 404, 422}
                     or status_code is not None and 300 <= status_code < 400)
            uncertain = isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
                "APITimeoutError", "APIConnectionError", "ReadTimeout", "ConnectError"}
            store.settle(eid, status="unknown" if uncertain else "failed", usage=usage,
                         detail=type(exc).__name__ + ("; external outcome uncertain" if uncertain else ""))
            if fatal:
                raise FatalModelError("模型鉴权、额度或请求配置错误；停止重试，请检查服务配置") from exc
            last = exc
            if _ + 1 < retries:
                sleep(min(2 * (_ + 1), 4))
    raise RunError(f"模型调用重试用尽：{type(last).__name__}") from last
