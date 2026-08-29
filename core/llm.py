"""LLM 客户端：OpenAI 兼容接口。

Key/接入点/模型通过 core.credentials 动态解析（系统凭据管理器优先），
模块内不出现任何硬编码密钥。
"""
import json
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


def _is_billing(e: Exception) -> bool:
    m = str(e)
    return "Arrearage" in m or "overdue" in m.lower() or "欠费" in m


def _raise_friendly(e: Exception) -> None:
    """把账户类错误翻译成可操作的中文提示（这类错误重试没有意义）。"""
    if isinstance(e, AuthenticationError):
        raise RuntimeError(_AUTH_HINT) from e
    if _is_billing(e):
        raise RuntimeError(_BILLING_HINT) from e


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


def chat_json(system: str, user: str, retries: int = 3, enable_thinking: bool = False) -> dict:
    """带重试的 JSON 输出调用；鉴权/欠费类错误不重试，直接给出可操作提示。

    enable_thinking=True 供判断型环节（如语义聚类）使用：更准但慢一个量级，
    因此调用超时也相应放宽。
    """
    client, model = _client_and_model()
    if enable_thinking:
        client = OpenAI(api_key=client.api_key, base_url=client.base_url, timeout=300)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": enable_thinking},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            if isinstance(e, AuthenticationError) or _is_billing(e):
                _raise_friendly(e)
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
        return resp.choices[0].message.content
    except Exception as e:
        if isinstance(e, AuthenticationError) or _is_billing(e):
            _raise_friendly(e)
        raise
