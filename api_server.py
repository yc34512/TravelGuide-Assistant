"""FastAPI 服务：网页界面 + 任务接口。

接口：
    GET  /                     网页界面
    GET  /api/health           健康检查 + 知识库概览
    POST /api/research         发起攻略研究任务 {keyword, mode, force} -> {job_id}
    POST /api/trip             发起行程规划任务 {city, days, hotel, spots?, preferences?, budget?, preference_mode?} -> {job_id}
    POST /api/heat/refresh     发起城市热度刷榜任务 {city} -> {job_id}（元数据轻量采集）
    GET  /api/heat/{city}      查询城市实时热度榜（本周最火/长盛不衰/正在降温/平稳）
    GET  /api/jobs/{id}        轮询任务状态/进度/结果（含重启前的历史任务）
    POST /api/jobs/{id}/cancel 取消运行中的任务（攻略/行程通用）
    GET  /api/jobs/history     历史任务摘要（持久化档案）
    GET  /api/reports          历史报告列表
    GET  /api/reports/download 下载指定报告文件（防目录穿越）
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import KB_TTL_DAYS, REPORT_DIR
from core import knowledge
from service import heatrefresh, research, trip

app = FastAPI(
    title="旅游攻略助手",
    description=(
        "抖音 UGC 采集 + AI 攻略整合，来源可溯。\n\n"
        "面向 AI 智能体/自动化平台：本服务全部接口均为无状态 HTTP JSON，"
        "可直接按 OpenAPI 规范导入 Dify / Coze / GPTs Actions / n8n / LangChain 等。\n"
        "典型调用链：POST /api/research 发起 -> 每 20 秒 GET /api/jobs/{id} 轮询 "
        "-> status=done 时取 result.markdown 获得带来源引用的完整报告。"
    ),
    version="1.2.0",
)

_WEB_DIR = Path(__file__).parent / "web"


class ResearchIn(BaseModel):
    keyword: str
    mode: str = "standard"  # fast / standard / deep
    force: bool = False


class TripIn(BaseModel):
    city: str
    days: int = 2
    hotel: str = ""
    spots: list[str] | None = None  # 指定景点清单；缺省时自动圈定（混合候选验证）
    preferences: str = ""
    budget: float | None = None  # 总预算（元，不含大交通）；缺省不做预算控制
    preference_mode: str = "均衡"  # 省钱优先 / 体验优先 / 均衡


class HeatRefreshIn(BaseModel):
    city: str


@app.post("/api/heat/refresh", summary="发起城市热度刷榜（元数据轻量采集）")
def heat_refresh(body: HeatRefreshIn):
    """对城市热门景点做一轮元数据采集刷榜（本周最火/正在降温）；后台任务，用 /api/jobs/{id} 轮询。"""
    city = body.city.strip()
    if not city:
        raise HTTPException(status_code=400, detail="城市不能为空")
    job_id = heatrefresh.start_heat_refresh(city)
    return {"job_id": job_id}


@app.get("/api/heat/{city}", summary="查询城市实时热度榜（景点榜 + 美食榜）")
def city_heat(city: str):
    """返回该城最新热度快照：景点榜与美食榜并列（各自按热度降序，含趋势标签）；
    无数据返回空列表与引导提示。"""
    rows = knowledge.load_heat_snapshots(city.strip())
    ranking = [r for r in rows if r.get("kind") != "美食"]
    food_ranking = [r for r in rows if r.get("kind") == "美食"]
    return {
        "city": city.strip(),
        "ranking": ranking,
        "food_ranking": food_ranking,
        "hint": "" if rows else "暂无该城热度数据：先点“刷新榜单”跑一轮刷榜任务（约 3~5 分钟）",
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_WEB_DIR / "index.html")


@app.get("/api/health", summary="健康检查 + 知识库概览")
def health():
    return {"status": "ok", "kb": knowledge.stats(), "kb_ttl_days": KB_TTL_DAYS}


@app.post("/api/research", summary="发起攻略研究任务")
def start_research(body: ResearchIn):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    job_id = research.start_job(keyword, mode=body.mode, force=body.force)
    return {"job_id": job_id}


@app.post("/api/trip", summary="发起行程规划任务（候选验证/调研/预算控制/排行程）")
def start_trip_api(body: TripIn):
    city = body.city.strip()
    if not city:
        raise HTTPException(status_code=400, detail="城市不能为空")
    if not 1 <= body.days <= 7:
        raise HTTPException(status_code=400, detail="天数需在 1~7 之间")
    if body.budget is not None and not 0 < body.budget <= 10_000_000:
        raise HTTPException(status_code=400, detail="预算需在 0~1000 万元之间")
    mode = body.preference_mode.strip() or "均衡"
    if mode not in ("省钱优先", "体验优先", "均衡"):
        raise HTTPException(status_code=400, detail="消费偏好仅支持：省钱优先 / 体验优先 / 均衡")
    job_id = trip.start_trip(
        city, body.days, body.hotel.strip(), body.spots, body.preferences.strip(),
        budget=body.budget, preference_mode=mode,
    )
    return {"job_id": job_id}


@app.get("/api/jobs/history", summary="历史任务摘要（重启后仍可查）")
def jobs_history():
    """历史任务摘要（服务重启后仍可查看）。注意：必须定义在 /api/jobs/{job_id} 之前，
    否则会被路径参数路由截胡。"""
    return {"jobs": knowledge.list_jobs()}


@app.get("/api/jobs/{job_id}", summary="查询任务状态/进度/结果")
def job_status(job_id: str):
    job = research.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.post("/api/jobs/{job_id}/cancel", summary="取消运行中的任务")
def cancel_job(job_id: str):
    """取消运行中的任务。已在终态或不存在时返回 409。"""
    if research.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not research.cancel_job(job_id):
        raise HTTPException(status_code=409, detail="任务不在运行中，无法取消")
    return {"ok": True}


@app.get("/api/reports", summary="历史报告列表")
def report_history():
    """历史报告列表（知识库登记且文件仍存在的）。"""
    return {"reports": knowledge.list_history()}


@app.get("/api/reports/download", summary="下载报告（Markdown/HTML，防目录穿越）")
def download_report(name: str):
    """按文件名下载报告（.md 或行程可视化 .html）。路径先 resolve 再校验父目录，防止 ../ 目录穿越。"""
    path = (REPORT_DIR / name).resolve()
    if path.parent != REPORT_DIR.resolve() or path.suffix not in (".md", ".html") or not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    media = "text/html" if path.suffix == ".html" else "text/markdown"
    return FileResponse(path, filename=path.name, media_type=media)
