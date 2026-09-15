"""服务启动入口：python run_server.py（或双击 运行服务.bat）。

启动前按顺序做三件事，把问题消灭在启动阶段：

1. 端口上已有**本服务**在跑 → 不重复启动，直接打开浏览器复用（可反复双击）；
2. 端口被其他程序占用 → 给出明确处理办法，不抛裸栈；
3. 缺 API Key → 就地弹出配置向导（环境不可交互时退回一行提示，不拦启动）。

浏览历史报告等只读功能不依赖 Key。
"""
import json
import socket
import threading
import urllib.request
import webbrowser

import uvicorn

from config import SERVER_HOST, SERVER_PORT

URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


def _ensure_key() -> None:
    """没有 Key 就走一次配置向导；放弃/不可交互时给出补配指引。"""
    try:
        from core.credentials import get_llm_config, interactive_setup
        if get_llm_config() is not None:
            return
        print("=" * 62)
        print("⚠️  未检测到 LLM API Key —— 没有它任何任务都会失败（浏览历史不受影响）。")
        print("   现在可以直接在下方完成配置：Key 存进系统凭据管理器，")
        print("   不落项目文件、不会被提交到 Git，只需一次。")
        print("=" * 62)
        if interactive_setup() is not None:
            print("配置完成，继续启动服务…\n")
            return
        print("未完成配置：服务将照常启动，但提交任务会失败。")
        print("之后可随时执行  python run_cli.py setup  补配；不想启动请按 Ctrl+C。\n")
    except Exception:
        pass          # 凭据库不可用不该拦住服务启动


def _service_already_running(url: str = URL, timeout: float = 1.5) -> bool:
    """端口上是否已有**本服务**在跑。

    只有 /api/health 返回 200 且 JSON 里 status == "ok" 才算——
    避免把恰好占用同端口的其他程序误判成"已在运行"。
    """
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
    except Exception:
        return False


def _port_in_use(host: str = SERVER_HOST, port: int = SERVER_PORT,
                 timeout: float = 1.0) -> bool:
    """端口是否已被占用（不区分占用者是谁）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    print(f"旅游攻略助手服务启动中：{URL} （按 Ctrl+C 停止）")

    if _service_already_running():
        print("检测到服务已在运行 —— 直接打开浏览器复用，不重复启动。")
        webbrowser.open(URL)
        return 0

    if _port_in_use():
        print("=" * 62)
        print(f"⚠️  端口 {SERVER_PORT} 已被其他程序占用，服务无法启动。")
        print("   处理办法（任选其一）：")
        print("   ① 关闭占用该端口的程序后重试；")
        print("   ② 在 .env 里改 SERVER_PORT（例如 8001）后重新双击本脚本。")
        print("=" * 62)
        return 1

    _ensure_key()

    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    try:
        uvicorn.run("api_server:app", host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    except SystemExit as e:                   # uvicorn 启动期错误（如端口占用）以 sys.exit 结束
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code == 0:                         # 正常退出（如 Ctrl+C 后优雅关闭）：不当作失败
            return 0
        print("=" * 62)
        print("[X] 服务启动失败（最常见原因：端口被占用）。")
        print(f"    处理：① 关闭占用 {SERVER_PORT} 端口的程序；"
              f"或 ② 在 .env 改 SERVER_PORT 后重试。")
        print("=" * 62)
        return 1
    except OSError as e:
        print(f"[X] 服务启动失败：{e}")
        print("    若为端口占用，请关闭占用程序或在 .env 修改 SERVER_PORT 后重试。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())