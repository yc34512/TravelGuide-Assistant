"""服务启动入口：python run_server.py（或双击 运行服务.bat）。

启动后自动打开浏览器进入 http://127.0.0.1:8000

注意：API Key 的**配置向导只在 CLI 入口**（运行.bat / python run_cli.py setup）。
直接启服务不会弹向导，所以这里在缺少 Key 时明确提示——否则用户要等到提交任务
失败、看到报错，才知道得回头去配一次。
"""
import threading
import webbrowser

import uvicorn

from config import SERVER_HOST, SERVER_PORT

URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


def _warn_if_no_key() -> None:
    try:
        from core.credentials import get_llm_config
        if get_llm_config() is None:
            print("=" * 60)
            print("⚠️  未检测到 LLM API Key —— 提交任务会在第一步就失败。")
            print("   请先配置（只需一次，Key 存入系统凭据管理器，不落项目文件）：")
            print("       python run_cli.py setup")
            print("   （Windows 也可双击 运行.bat，首次会自动弹出配置向导）")
            print("=" * 60)
    except Exception:
        pass          # 凭据库不可用不该拦住服务启动


if __name__ == "__main__":
    print(f"旅游攻略助手服务启动中：{URL} （按 Ctrl+C 停止）")
    _warn_if_no_key()
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    uvicorn.run("api_server:app", host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
