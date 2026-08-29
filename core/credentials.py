"""API Key 的安全存取（为开源发布设计）。

设计目标：仓库里永远没有密钥。优先级链：

1. 系统凭据管理器（Windows 凭据管理器 / macOS 钥匙串 / Linux SecretService）
   —— Key 只存在操作系统级的本机加密存储中，不落在任何项目文件里，
      天然不可能被误提交到 Git；
2. .env 文件（兼容偏好文件配置的用户；属明文存储，README 已注明风险，
   且 .env 在 .gitignore 中被排除）；
3. 两者都无 -> 返回 None，由调用方触发交互式配置向导（见 interactive_setup）。
"""
import getpass
import json

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

try:
    import keyring
except Exception:  # 个别无凭据服务的 Linux 环境可能不可用
    keyring = None

SERVICE = "TravelGuideAssistant"
_ITEM = "llm_config"


class NoApiKeyError(RuntimeError):
    """未配置任何 API Key。"""


# 常见 OpenAI 兼容服务商预设：(名称, base_url, 默认模型)
PROVIDERS = [
    ("阿里云百炼 DashScope（通义千问）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("Moonshot Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
]


def get_llm_config() -> dict | None:
    """按优先级返回 {"api_key", "base_url", "model"}，没有则 None。"""
    if keyring is not None:
        try:
            blob = keyring.get_password(SERVICE, _ITEM)
            if blob:
                cfg = json.loads(blob)
                if cfg.get("api_key"):
                    return cfg
        except Exception:
            pass
    if LLM_API_KEY:  # .env 备选通道
        return {
            "api_key": LLM_API_KEY,
            "base_url": LLM_BASE_URL,
            "model": LLM_MODEL,
        }
    return None


def save_llm_config(api_key: str, base_url: str, model: str) -> None:
    if keyring is None:
        raise RuntimeError(
            "本系统没有可用的凭据管理器。请改用 .env 方式配置（见 README 安全说明）。"
        )
    keyring.set_password(
        SERVICE,
        _ITEM,
        json.dumps({"api_key": api_key, "base_url": base_url, "model": model}),
    )


def delete_llm_config() -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, _ITEM)
    except Exception:
        pass  # 本来就不存在


def _validate(api_key: str, base_url: str, model: str) -> None:
    """发一次最小请求验证 Key。失败抛异常——无效的 Key 绝不入库。"""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=25)
    client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=3,
    )


def interactive_setup(force: bool = False) -> dict | None:
    """首次运行/重新配置向导：选服务商 -> 掩码输入 Key -> 验证 -> 存入系统凭据库。

    返回配置 dict；用户中途退出返回 None。
    """
    if force:
        delete_llm_config()
    else:
        existing = get_llm_config()
        if existing:
            return existing

    print("\n" + "=" * 52)
    print("  首次使用，需要配置 AI 服务（只需一次）")
    print("  Key 将保存进本机系统凭据管理器，")
    print("  不会写入项目文件，也不会被提交到 Git。")
    print("=" * 52)
    for i, (name, _, _) in enumerate(PROVIDERS, 1):
        print(f"  {i}. {name}")
    print("  5. 其他 OpenAI 兼容服务（自定义接入点）")

    try:
        while True:
            choice = (input("选择服务商 [1-5]（回车默认 1）: ").strip() or "1")
            if choice not in {"1", "2", "3", "4", "5"}:
                print("无效选择，请重试。")
                continue
            if choice == "5":
                base_url = input("base_url（如 https://xxx.com/v1）: ").strip()
                model = input("模型名（如 qwen-plus）: ").strip()
            else:
                _, base_url, model = PROVIDERS[int(choice) - 1]
                custom = input(f"模型名（回车用默认 {model}）: ").strip()
                if custom:
                    model = custom
            api_key = getpass.getpass("粘贴 API Key（输入不回显）: ").strip()
            if not api_key:
                print("Key 不能为空，请重试。")
                continue
            print("正在验证 Key…")
            try:
                _validate(api_key, base_url, model)
            except Exception as e:
                print(f"[X] 验证失败，Key 未保存：{str(e)[:120]}")
                print("    请确认 Key 与服务商匹配后重试（Ctrl+C 可退出）。\n")
                continue
            save_llm_config(api_key, base_url, model)
            print("[OK] 验证通过，Key 已安全保存到系统凭据管理器。")
            print(f"     接入点: {base_url}  模型: {model}\n")
            return {"api_key": api_key, "base_url": base_url, "model": model}
    except (EOFError, KeyboardInterrupt):
        print("\n未完成配置；之后运行时会再次提示。")
        return None
