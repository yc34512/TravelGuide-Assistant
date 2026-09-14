"""MCP 服务器：把旅游攻略助手暴露为标准工具，供任意 MCP 兼容智能体调用。

适用客户端：Claude Desktop / Cursor / Cherry Studio / Cline / Qoder / WorkBuddy 等。
以 stdio 方式接入，客户端配置示例：

    {
      "mcpServers": {
        "travel-guide": {
          "command": "python",
          "args": ["C:/路径/到项目/mcp_server.py"]
        }
      }
    }

目标服务地址用环境变量 TG_SERVER_URL 覆盖；缺省时取项目 config 里的
SERVER_HOST / SERVER_PORT（与 run_server.py 起在同一处，即 http://127.0.0.1:8000）。

**本进程会按需自动拉起本地服务**：任何工具调用都先探一次 /api/health，不通就在
后台起一个 uvicorn（刻意不弹浏览器），就绪后继续执行原调用——客户端因此不需要
先手动启动服务。拉不起来时返回友好的排障指引，而不是抛栈。
"""
import asyncio
import os
import urllib.parse
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

PROJECT_DIR = Path(__file__).resolve().parent
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_BOOT_WAIT = 20                 # 冷启动最多等 20 秒（实测通常 2~4 秒）
_AUTOSTART_LOCK = asyncio.Lock()
_autostart_tried = False        # 本进程只尝试拉起一次，避免连续失败时反复 spawn


class ServiceUnavailable(RuntimeError):
    """服务不可用（含自动拉起失败），携带给用户看的排障说明。"""


def _resolve_target() -> tuple[str, str, int, bool]:
    """定出 (base_url, host, port, 是否本机)。

    端口缺省时回落到项目 config，保证与 run_server.py 起在同一地址；主机名按
    TG_SERVER_URL 解析，用于判断"要不要在本地自动拉起"——指向远端服务时不该
    在本机乱起进程。
    """
    try:
        from config import SERVER_HOST, SERVER_PORT
    except Exception:                     # 单独拷走此文件时也能跑
        SERVER_HOST, SERVER_PORT = "127.0.0.1", 8000
    env = os.getenv("TG_SERVER_URL", "").strip()
    if not env:
        return f"http://{SERVER_HOST}:{SERVER_PORT}", SERVER_HOST, SERVER_PORT, True
    p = urllib.parse.urlparse(env)
    host = p.hostname or SERVER_HOST
    return env.rstrip("/"), host, p.port or SERVER_PORT, host in _LOCAL_HOSTS


BASE_URL, _HOST, _PORT, _IS_LOCAL = _resolve_target()

mcp = FastMCP("travel-guide-assistant")

_START_HINT = (
    "服务未启动或不可达。可手动启动旅游攻略助手服务：在项目目录执行 "
    "python run_server.py（Windows 可双击 运行服务.bat），等待 3 秒后重试。"
)


def _client(**kw) -> httpx.AsyncClient:
    """统一的 httpx 客户端。

    trust_env=False 对本机地址是关键：本机请求绝不能走 HTTP_PROXY。企业代理、
    容器/沙箱或任何设了环境代理的机器上，代理对 127.0.0.1 的处理各不相同
    （实测常见的是直接回 502），会表现为"服务明明起着却调不通"这种极难排查的
    假故障。只有指向远端服务（TG_SERVER_URL 非本机）时才允许读环境代理。
    """
    return httpx.AsyncClient(base_url=BASE_URL, trust_env=not _IS_LOCAL, **kw)


async def _healthy(timeout: float = 2.0) -> bool:
    """探一次 /api/health（轻量、短超时，仅用于判断要不要拉起服务）。"""
    try:
        async with _client(timeout=timeout) as c:
            return (await c.get("/api/health")).status_code == 200
    except Exception:
        return False


def _autostart_log() -> Path:
    """自动拉起服务的日志落点。

    刻意不丢 DEVNULL：服务若是"起来了又立刻死"，DEVNULL 会让失败彻底不可诊断
    （这正是本项目早期 Agent 包装最难受的一点）。写到 data/debug/ 下，出问题
    时能直接看栈。目录不可写则退回落项目根的单个日志文件。
    """
    d = PROJECT_DIR / "data" / "debug"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d / "server_autostart.log"
    except Exception:
        return PROJECT_DIR / "server_autostart.log"


def _spawn_server() -> str | None:
    """后台拉起本地服务。成功返回 None，失败返回原因。

    刻意直接起 uvicorn 而不是跑 run_server.py：后者会在 1.5 秒后
    `webbrowser.open()` 弹出浏览器窗口——由智能体触发的启动不该抢用户的焦点。

    进程与 MCP 客户端解耦（Windows DETACHED_PROCESS / POSIX start_new_session）：
    客户端退出后服务继续存活，下次调用直接命中，不必再等一次冷启动。
    """
    import subprocess
    import sys

    cmd = [sys.executable, "-m", "uvicorn", "api_server:app",
           "--host", _HOST, "--port", str(_PORT), "--log-level", "warning"]
    try:
        log = open(_autostart_log(), "ab", buffering=0)   # noqa: SIM115 —— 交给子进程持有
        kwargs: dict = {"cwd": str(PROJECT_DIR),
                        "stdin": subprocess.DEVNULL,
                        "stdout": log, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return None
    except Exception as e:                      # 拉不起来也不抛栈，交回排障话术
        return str(e)


async def _ensure_service() -> str | None:
    """确保本地服务可用：健康则直接过；否则自动拉起一次并等待就绪。

    返回 None 表示可用，否则返回给用户看的排障说明。
    只对**本机**地址自动拉起——TG_SERVER_URL 指向远端服务时不该在本地乱起进程。
    """
    global _autostart_tried
    if await _healthy():
        return None
    if not _IS_LOCAL:
        return _START_HINT
    async with _AUTOSTART_LOCK:
        if await _healthy():            # 等锁期间可能已被别的调用拉起来了
            return None
        if _autostart_tried:            # 本进程只尝试一次，避免连续失败时反复 spawn
            return _START_HINT
        _autostart_tried = True
        if err := _spawn_server():
            return f"{_START_HINT}（自动拉起失败：{err}）"
        for _ in range(_BOOT_WAIT):     # 冷启动通常 2~4 秒
            await asyncio.sleep(1)
            if await _healthy():
                return None
    return f"{_START_HINT}（已尝试自动拉起，但 {_BOOT_WAIT} 秒内仍未就绪，请手动排查）"


async def _get(path: str, params: dict | None = None):
    if hint := await _ensure_service():
        raise ServiceUnavailable(hint)
    async with _client(timeout=30) as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict | None = None):
    if hint := await _ensure_service():
        raise ServiceUnavailable(hint)
    async with _client(timeout=30) as c:
        r = await c.post(path, json=body or {})
        r.raise_for_status()
        return r.json()


def _conn_err(e: Exception) -> dict:
    if isinstance(e, ServiceUnavailable):
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": _START_HINT, "detail": str(e)}


@mcp.tool()
async def check_service() -> dict:
    """检查旅游攻略助手服务是否在线，返回知识库概览（景点数/报告数）。"""
    try:
        return await _get("/api/health")
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def start_research(keyword: str, mode: str = "standard", force: bool = False) -> dict:
    """发起景点攻略研究任务。

    keyword: 景点关键词，如"西湖"。
    mode: fast(约3分钟/5视频) | standard(约6分钟/8视频) | deep(约15~25分钟/10视频+口播转写)。
    force: true 跳过知识库缓存强制重新采集；默认 false（7天内重复查询约30秒命中缓存）。
    返回 job_id 与预估耗时；任务在后台运行，用 get_job_status 轮询。
    """
    try:
        data = await _post("/api/research", {"keyword": keyword, "mode": mode, "force": force})
        eta = {"fast": "约3分钟", "standard": "约6分钟", "deep": "约15~25分钟"}.get(mode, "约6分钟")
        return {"ok": True, "job_id": data["job_id"], "estimated": eta,
                "hint": "任务后台运行，请每20秒调用 get_job_status 轮询，不要同步等待。"}
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def plan_trip(city: str, days: int = 2, hotel: str = "",
                    spots: str = "", preferences: str = "",
                    start_date: str = "", preference_mode: str = "均衡") -> dict:
    """发起多天行程规划任务：先提炼高赞攻略视频的行程草案（主干），再圈定候选并经抖音验证筛选、逐个调研、按顺路原则排线。

    city: 目的地城市，如"大同"。days: 出行天数 1~7。hotel: 酒店/住宿位置（用于排线）。
    spots: 可选，指定景点用逗号分隔；留空则自动圈定并验证。preferences: 可选偏好（如"带老人"）。
    start_date: 可选，出发日期（YYYY-MM-DD）；传入后校验闭馆日。
    preference_mode: 省钱优先 / 体验优先 / 均衡（默认）。
    输出含逐日路书、避坑专题（附评论原文）、热度榜、美食榜与 HTML 可视化路书；不做预算估算（预算由用户自行考虑）。
    耗时较长：未调研过的景点约 5 分钟/个，调研过的秒级命中缓存。用 get_job_status 轮询。
    """
    body = {"city": city, "days": days, "hotel": hotel, "preferences": preferences,
            "preference_mode": preference_mode}
    if start_date.strip():
        body["start_date"] = start_date.strip()
    if spots.strip():
        body["spots"] = [s.strip() for s in spots.replace("、", ",").split(",") if s.strip()]
    try:
        data = await _post("/api/trip", body)
        return {"ok": True, "job_id": data["job_id"],
                "hint": "行程任务耗时较长，请每 30 秒调用 get_job_status 轮询，不要同步等待。"}
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def get_city_heat(city: str) -> dict:
    """查询城市实时热度榜（本周最火/长盛不衰/正在降温）：景点榜 ranking 与美食榜
    food_ranking 并列返回。数据来自最近一轮刷榜；若返回空榜单，可先调 refresh_city_heat 采集刷新。
    """
    try:
        return await _get(f"/api/heat/{city}")
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def refresh_city_heat(city: str) -> dict:
    """发起城市热度刷榜任务：对热门景点与代表性美食做元数据轻量采集（只取点赞与发布时间，
    不采评论），约 3~5 分钟。用 get_job_status 轮询，完成后再调 get_city_heat 看榜单。"""
    try:
        data = await _post("/api/heat/refresh", {"city": city})
        return {"ok": True, "job_id": data["job_id"],
                "hint": "刷榜任务后台运行，请每 20 秒调用 get_job_status 轮询。"}
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """查询任务进度。返回 status：running(继续轮询) / done(成功,结果在 result.markdown) /
    error(失败,原因在 error) / cancelled(已取消)。日志只保留末尾 8 行以节省上下文。"""
    try:
        job = await _get(f"/api/jobs/{job_id}")
        job["log"] = (job.get("log") or [])[-8:]
        return job
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"任务不存在或已清理（{e.response.status_code}）"}
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def cancel_research(job_id: str) -> dict:
    """取消运行中的任务（当前步骤结束后生效，浏览器资源自动释放）。非运行态任务会返回失败说明。"""
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
            r = await c.post(f"/api/jobs/{job_id}/cancel")
            if r.status_code == 200:
                return {"ok": True, "hint": "取消请求已受理，稍后轮询到 cancelled 即生效。"}
            return {"ok": False, "error": r.json().get("detail", str(r.status_code))}
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def list_reports() -> dict:
    """列出历史攻略报告（关键词、采集时间、视频/评论数、报告文件名）。"""
    try:
        return await _get("/api/reports")
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def get_report_content(report_name: str) -> dict:
    """读取指定历史报告的完整 Markdown 内容。report_name 用 list_reports 返回的 report_path 字段。"""
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
            r = await c.get("/api/reports/download", params={"name": report_name})
            if r.status_code != 200:
                return {"ok": False, "error": "报告不存在或已被删除"}
            return {"ok": True, "markdown": r.text}
    except Exception as e:
        return _conn_err(e)


if __name__ == "__main__":
    mcp.run()  # stdio 传输：由 MCP 客户端以子进程方式拉起
