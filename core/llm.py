"""LLM 客户端：OpenAI 兼容接口。

Key/接入点/模型通过 core.credentials 动态解析（系统凭据管理器优先），
模块内不出现任何硬编码密钥。

成本约束：
1. 联网搜索默认关闭（config.LLM_WEB_SEARCH=False）——部分服务商的搜索插件
   属独立计费、不在资源包抵扣范围内，默认不发联网参数；
2. 识别额度耗尽（403 AllocationQuota.FreeTierOnly）：属额度类错误而非故障，
   重试无意义，直接给可操作提示；
3. 进程级 Token 用量账本（usage_note）：每个任务如实报消耗，便于核对。
"""
import json
import threading
import time

from openai import AuthenticationError, OpenAI

from core.credentials import NoApiKeyError, get_llm_config

_AUTH_HINT = (
    "API Key 无效或已过期。重新配置方法：双击 运行.bat 输入 setup，"
    "或在命令行执行 python run_cli.py setup"
)
_BILLING_HINT = (
    "LLM 账户欠费或免费额度已用尽：请到服务商控制台充值/领取额度后重试，"
    "或运行 python run_cli.py setup 换一家服务商。"
)
# 百炼「免费额度用完即停」（安心模式）额度耗尽时返回 403 + 该错误码。
# 开了它就不会转按量付费，额度耗尽即停止服务。
_FREE_TIER_CODE = "AllocationQuota.FreeTierOnly"
_FREE_TIER_HINT = (
    "本模型的百炼免费额度已用尽，且控制台开着「免费额度用完即停」（安心模式），"
    "所以直接拒绝调用——这是防止扣你现金余额的保护，不是故障。\n"
    "两条出路：\n"
    "  ① 换一个仍有免费额度的模型（每个模型各有独立 100 万 Token，"
    "百炼控制台「免费额度」页可查余量与到期时间），"
    "执行 python run_cli.py setup 重新配置；\n"
    "  ② 若你愿意用代金券/余额付费，到控制台「免费额度」页关掉该模型的用完即停开关"
    "（生效有延迟，约半小时）。"
)


def _is_billing(e: Exception) -> bool:
    m = str(e)
    return "Arrearage" in m or "overdue" in m.lower() or "欠费" in m


def _is_free_tier_exhausted(e: Exception) -> bool:
    """免费额度耗尽且开了用完即停（403）。与欠费区分开：后者要充值，前者只要换模型。"""
    s = str(e)
    return _FREE_TIER_CODE in s or "FreeTierOnly" in s


def _is_account_fatal(e: Exception) -> bool:
    """账户/额度态错误：重试没有意义（只会白等三次），必须直接翻译成可操作提示。"""
    return isinstance(e, AuthenticationError) or _is_billing(e) or _is_free_tier_exhausted(e)


def _raise_friendly(e: Exception) -> None:
    """把账户类错误翻译成可操作的中文提示（这类错误重试没有意义）。"""
    if isinstance(e, AuthenticationError):
        raise RuntimeError(_AUTH_HINT) from e
    if _is_free_tier_exhausted(e):
        raise RuntimeError(_FREE_TIER_HINT) from e
    if _is_billing(e):
        raise RuntimeError(_BILLING_HINT) from e


# —— 进程级 Token 用量账本（成本可见性）——
# 并发采集下多个线程同时调用，所以记账加锁；只统计成功调用（失败不耗额度）。
_USAGE_LOCK = threading.Lock()
_USAGE: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "by_model": {}}


def _record_usage(model: str, usage) -> None:
    """累加一次调用的 token 消耗。usage 为 None（个别服务商不返）时只计次数。"""
    p = int(getattr(usage, "prompt_tokens", 0) or 0)
    c = int(getattr(usage, "completion_tokens", 0) or 0)
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["prompt_tokens"] += p
        _USAGE["completion_tokens"] += c
        m = _USAGE["by_model"].setdefault(model, {"calls": 0, "tokens": 0})
        m["calls"] += 1
        m["tokens"] += p + c


def usage_summary() -> dict:
    """本进程累计的 LLM 调用与 token 消耗（供任务日志与报告 meta 使用）。"""
    with _USAGE_LOCK:
        return {
            "calls": _USAGE["calls"],
            "prompt_tokens": _USAGE["prompt_tokens"],
            "completion_tokens": _USAGE["completion_tokens"],
            "total_tokens": _USAGE["prompt_tokens"] + _USAGE["completion_tokens"],
            "by_model": {k: dict(v) for k, v in _USAGE["by_model"].items()},
        }


def usage_note() -> str:
    """一行可读的用量汇总（写进任务日志，让用户能自己核额度）。"""
    s = usage_summary()
    if not s["calls"]:
        return "LLM 用量：本次未调用"
    per = "、".join(f"{k} {v['tokens']} token/{v['calls']} 次"
                   for k, v in sorted(s["by_model"].items()))
    return (f"LLM 用量（本进程累计）：{s['calls']} 次调用、共 {s['total_tokens']} token"
            f"（输入 {s['prompt_tokens']} + 输出 {s['completion_tokens']}）｜{per}")


def reset_usage() -> None:
    """清零用量账本（离线测试用；服务进程不需要，累计值就是全量成本）。"""
    with _USAGE_LOCK:
        _USAGE["calls"] = 0
        _USAGE["prompt_tokens"] = 0
        _USAGE["completion_tokens"] = 0
        _USAGE["by_model"] = {}


def _client_and_model() -> tuple[OpenAI, str]:
    cfg = get_llm_config()
    if not cfg:
        raise NoApiKeyError(
            "未配置 API Key。请重新运行 运行.bat，按提示配置（只需一次，"
            "Key 会存进系统凭据管理器）。"
        )
    return OpenAI(api_key=cfg["api_key"], base_url=cfg.get("base_url"), timeout=120), (
        cfg.get("model") or "qwen-turbo"
    )


# qwen3.x 混合推理模型默认开启思考模式，会产生超长推理链（大输入下单次数分钟）。
# 本管道是确定性结构化抽取，思考没有收益只有延迟，统一关闭。
_EXTRA_BODY = {"enable_thinking": False}

# 联网搜索能力探测：服务商不支持联网参数时置 True，后续调用不再携带（降级为纯基线）。
# 兼容键同时下发两系：通义/百炼系认 enable_search，智谱 glm 系认 search.enable；
# 不认识的键会触发 400 参数错误，被探测逻辑捕获后去参重试。
_WEB_SEARCH_UNSUPPORTED = False


def _is_param_err(e: Exception) -> bool:
    """参数类错误（400/invalid request）：联网参数不兼容的典型信号，重试无意义。"""
    if type(e).__name__ == "BadRequestError":
        return True
    m = str(e).lower()
    return "invalid_request" in m or "unsupported parameter" in m or "unknown parameter" in m


def _extra_body(enable_thinking: bool, web_search: bool) -> dict:
    body = {"enable_thinking": enable_thinking}
    if web_search and not _WEB_SEARCH_UNSUPPORTED:
        body["enable_search"] = True
        body["search"] = {"enable": True}
    return body


def chat_json(system: str, user: str, retries: int = 3, enable_thinking: bool = False,
              web_search: bool = False) -> dict:
    """带重试的 JSON 输出调用；鉴权/欠费类错误不重试，直接给出可操作提示。

    enable_thinking=True 供判断型环节（如语义聚类）使用：更准但慢一个量级，
    因此调用超时也相应放宽。
    web_search=True 尝试启用服务商联网搜索；服务商不支持时自动去参降级（本进程内不再尝试）。
    """
    global _WEB_SEARCH_UNSUPPORTED
    client, model = _client_and_model()
    if enable_thinking:
        client = OpenAI(api_key=client.api_key, base_url=client.base_url, timeout=300)
    use_ws = web_search and not _WEB_SEARCH_UNSUPPORTED
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                extra_body=_extra_body(enable_thinking, use_ws),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            _record_usage(model, getattr(resp, "usage", None))
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            if _is_account_fatal(e):
                _raise_friendly(e)      # 鉴权/欠费/免费额度耗尽：不重试，直接给可操作提示
            if use_ws and _is_param_err(e):
                # 服务商不认联网参数：记下降级标记，去参立即重试（不消耗重试配额）
                _WEB_SEARCH_UNSUPPORTED = True
                use_ws = False
                continue
            last_err = e  # 网络抖动 / 偶发 JSON 不合法才重试
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM JSON 调用失败：{last_err}")


def chat_text(system: str, user: str, temperature: float = 0.3) -> str:
    """普通文本生成调用（用于报告撰写）。"""
    client, model = _client_and_model()
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            extra_body=_EXTRA_BODY,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        _record_usage(model, getattr(resp, "usage", None))
        return resp.choices[0].message.content
    except Exception as e:
        if _is_account_fatal(e):
            _raise_friendly(e)
        raise
