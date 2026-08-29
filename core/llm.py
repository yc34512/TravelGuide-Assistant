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


def _client_and_model() -> tuple[OpenAI, str]:
    cfg = get_llm_config()
    if not cfg:
        raise NoApiKeyError(
            "未配置 API Key。请重新运行 运行.bat，按提示配置（只需一次，"
            "Key 会存进系统凭据管理器）。"
        )
    return OpenAI(api_key=cfg["api_key"], base_url=cfg.get("base_url")), (
        cfg.get("model") or "qwen-turbo"
    )


def chat_json(system: str, user: str, retries: int = 3) -> dict:
    """带重试的 JSON 输出调用；鉴权失败不重试，直接给出可操作的提示。"""
    client, model = _client_and_model()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return json.loads(resp.choices[0].message.content)
        except AuthenticationError:
            raise RuntimeError(_AUTH_HINT)
        except Exception as e:  # 网络抖动 / 偶发 JSON 不合法都重试
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM JSON 调用失败：{last_err}")


def chat_text(system: str, user: str, temperature: float = 0.3) -> str:
    """普通文本生成调用（用于报告撰写）。"""
    client, model = _client_and_model()
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except AuthenticationError:
        raise RuntimeError(_AUTH_HINT)
    return resp.choices[0].message.content
