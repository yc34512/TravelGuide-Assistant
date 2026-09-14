"""服务启动入口：python run_server.py（或双击 运行服务.bat）。

启动后自动打开浏览器进入 http://127.0.0.1:8000

缺少 API Key 时**就地弹出配置向导**（双击场景有可交互终端，与 运行.bat 是同一个向导）；
用户放弃、或环境不可交互（被其他程序以无终端方式拉起、stdin 已关闭）时退回一行提示，
不拦住服务启动——浏览历史报告等只读功能不依赖 Key。
"""
import threading
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


if __name__ == "__main__":
    print(f"旅游攻略助手服务启动中：{URL} （按 Ctrl+C 停止）")
    _ensure_key()
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    uvicorn.run("api_server:app", host=SERVER_HOST, port=SERVER_PORT, log_level="warning")