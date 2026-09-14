"""FastAPI 服务：网页界面 + 任务接口。

接口：
    GET  /                     网页界面
    GET  /api/health           健康检查 + 知识库概览
    POST /api/research         发起攻略研究任务 {keyword, mode, force} -> {job_id}
    POST /api/trip             发起行程规划任务 {city, days, hotel, spots?, preferences?, preference_mode?, start_date?} -> {job_id}
    POST /api/heat/refresh     发起城市热度刷榜任务 {city} -> {job_id}（元数据轻量采集）
    POST /api/ask              就一份已生成的报告追问 {question, job_id?/report_name?} -> {answer}
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
from core import geo, knowledge
from pipeline import ask as ask_engine
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
    preference_mode: str = "均衡"  # 省钱优先 / 体验优先 / 均衡（影响选点倾向，不参与预算计算）
    start_date: str | None = None  # 出发日 YYYY-MM-DD（选填）：提供后 R3 按官方闭馆日真校行程


class HeatRefreshIn(BaseModel):
    city: str


class AskIn(BaseModel):
    question: str
    job_id: str | None = None          # 刚生成的报告：前端手里已有 job_id
    report_name: str | None = None     # 从历史列表点开的报告：用文件名反查任务档案
    history: list[dict] | None = None  # 最近几轮追问 [{q, a}]，用于多轮连续


@app.post("/api/ask", summary="就一份已生成的报告追问（基于档案回答，不重新采集）")
def ask_report(body: AskIn):
    """针对报告内容追问（"这个为什么值得去""两个只能选一个选哪个"）。

    答案来自该任务**已有的档案**（亮点/避雷/真实评价/热度/待确认项）：
    不触发采集、不开浏览器、不重排行程，成本仅一次 LLM 调用。
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    job = None
    if body.job_id:
        job = research.JOBS.get(body.job_id)
        if not job or not job.get("result"):
            job = knowledge.load_job(body.job_id) or job
    if (not job or not job.get("result")) and body.report_name:
        job = knowledge.find_job_by_report(body.report_name) or job
    if not job or not job.get("result"):
        raise HTTPException(
            status_code=404,
            detail="找不到这份报告的任务档案，无法追问（可重新生成一份后再问）",
        )
    try:
        text = ask_engine.answer(job["result"], question, body.history)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"追问失败：{e}")
    return {"answer": text, "job_id": job.get("id"), "keyword": job.get("keyword")}


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
    return {"status": "ok", "kb": knowledge.stats(), "kb_ttl_days": KB_TTL_DAYS,
            "amap": geo.amap_usage()}


@app.post("/api/research", summary="发起攻略研究任务")
def start_research(body: ResearchIn):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    job_id = research.start_job(keyword, mode=body.mode, force=body.force)
    return {"job_id": job_id}


@app.post("/api/trip", summary="发起行程规划任务（候选验证/调研/排行程）")
def start_trip_api(body: TripIn):
    city = body.city.strip()
    if not city:
        raise HTTPException(status_code=400, detail="城市不能为空")
    if not 1 <= body.days <= 7:
        raise HTTPException(status_code=400, detail="天数需在 1~7 之间")
    mode = body.preference_mode.strip() or "均衡"
    if mode not in ("省钱优先", "体验优先", "均衡"):
        raise HTTPException(status_code=400, detail="消费偏好仅支持：省钱优先 / 体验优先 / 均衡")
    job_id = trip.start_trip(
        city, body.days, body.hotel.strip(), body.spots, body.preferences.strip(),
        preference_mode=mode,
        start_date=(body.start_date or "").strip() or None,
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
@app.head("/api/reports/download", include_in_schema=False)
def download_report(name: str):
    """按文件名下载报告（.md 或行程可视化 .html）。路径先 resolve 再校验父目录，防止 ../ 目录穿越。

    同时挂 HEAD：网页端点历史里的行程报告时，先用 HEAD 探一下 HTML 路书是否存在——
    存在才开新标签页，否则会开出 404 空页（早期报告只留了 Markdown）。只回响应头，
    不传 100KB+ 正文，探针成本可忽略。
    """
    path = (REPORT_DIR / name).resolve()
    if path.parent != REPORT_DIR.resolve() or path.suffix not in (".md", ".html") or not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    media = "text/html" if path.suffix == ".html" else "text/markdown"
    # HTML 可视化版内联打开（新标签页直接渲染）；Markdown 保持下载
    disposition = "inline" if path.suffix == ".html" else "attachment"
    return FileResponse(path, filename=path.name, media_type=media,
                        content_disposition_type=disposition)
