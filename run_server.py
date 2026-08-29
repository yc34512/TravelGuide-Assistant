"""服务启动入口：python run_server.py（或双击 运行服务.bat）。

启动后自动打开浏览器进入 http://127.0.0.1:8000
"""
import threading
import webbrowser

import uvicorn

from config import SERVER_HOST, SERVER_PORT

URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

if __name__ == "__main__":
    print(f"旅游攻略助手服务启动中：{URL} （按 Ctrl+C 停止）")
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    uvicorn.run("api_server:app", host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
