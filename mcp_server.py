"""MCP 服务器：把旅游攻略助手暴露为标准工具，供任意 MCP 兼容智能体调用。

适用客户端：Claude Desktop / Cursor / Cherry Studio / Cline / Qoder 等。
以 stdio 方式接入，客户端配置示例：

    {
      "mcpServers": {
        "travel-guide": {
          "command": "python",
          "args": ["C:/路径/到项目/mcp_server.py"]
        }
      }
    }

目标服务地址用环境变量 TG_SERVER_URL 覆盖（默认 http://127.0.0.1:8000）。
本进程只做"翻译"：HTTP 调不通时返回友好的排障指引，而不是抛栈。
"""
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("TG_SERVER_URL", "http://127.0.0.1:8000")

mcp = FastMCP("travel-guide-assistant")

_START_HINT = (
    "服务未启动或不可达。请先启动旅游攻略助手服务：在项目目录执行 "
    "python run_server.py（Windows 可双击 运行服务.bat），等待 3 秒后重试。"
)


async def _get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict | None = None):
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
        r = await c.post(path, json=body or {})
        r.raise_for_status()
        return r.json()


def _conn_err(e: Exception) -> dict:
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
                    budget: float = 0, preference_mode: str = "均衡") -> dict:
    """发起多天行程规划任务：自动圈定候选并经抖音验证筛选、逐个调研、预算控制、按顺路原则排线。

    city: 目的地城市，如"大同"。days: 出行天数 1~7。hotel: 酒店/住宿位置（用于排线）。
    spots: 可选，指定景点用逗号分隔；留空则自动圈定并验证。preferences: 可选偏好（如"带老人"）。
    budget: 可选，总预算（元，不含大交通），传入后输出预算明细与超支预警。
    preference_mode: 省钱优先 / 体验优先 / 均衡（默认）。
    输出含逐日路书、预算明细、避坑专题（附评论原文）、热度榜与 HTML 可视化。
    耗时较长：未调研过的景点约 5 分钟/个，调研过的秒级命中缓存。用 get_job_status 轮询。
    """
    body = {"city": city, "days": days, "hotel": hotel, "preferences": preferences,
            "preference_mode": preference_mode}
    if budget > 0:
        body["budget"] = budget
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
    """查询城市实时热度榜（本周最火/正在降温）。
    数据来自最近一轮刷榜；若返回空榜单，可先调 refresh_city_heat 采集刷新。
    """
    try:
        return await _get(f"/api/heat/{city}")
    except Exception as e:
        return _conn_err(e)


@mcp.tool()
async def refresh_city_heat(city: str) -> dict:
    """发起城市热度刷榜任务：对热门景点做元数据轻量采集（只取点赞与发布时间，不采评论），
    约 3~5 分钟。用 get_job_status 轮询，完成后再调 get_city_heat 看榜单。"""
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
