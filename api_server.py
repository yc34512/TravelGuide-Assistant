"""FastAPI 服务：网页界面 + 任务接口。

接口：
    GET  /                     网页界面
    GET  /api/health           健康检查 + 知识库概览
    POST /api/research         发起研究任务 {keyword, mode, force} -> {job_id}
    GET  /api/jobs/{id}        轮询任务状态/进度/结果（含重启前的历史任务）
    POST /api/jobs/{id}/cancel 取消运行中的任务
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
from service import research

app = FastAPI(title="旅游攻略助手", description="抖音 UGC 采集 + AI 攻略整合，来源可溯")

_WEB_DIR = Path(__file__).parent / "web"


class ResearchIn(BaseModel):
    keyword: str
    mode: str = "standard"  # fast / standard / deep
    force: bool = False


@app.get("/")
def index():
    return FileResponse(_WEB_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "kb": knowledge.stats(), "kb_ttl_days": KB_TTL_DAYS}


@app.post("/api/research")
def start_research(body: ResearchIn):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    job_id = research.start_job(keyword, mode=body.mode, force=body.force)
    return {"job_id": job_id}


@app.get("/api/jobs/history")
def jobs_history():
    """历史任务摘要（服务重启后仍可查看）。注意：必须定义在 /api/jobs/{job_id} 之前，
    否则会被路径参数路由截胡。"""
    return {"jobs": knowledge.list_jobs()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = research.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """取消运行中的任务。已在终态或不存在时返回 409。"""
    if research.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not research.cancel_job(job_id):
        raise HTTPException(status_code=409, detail="任务不在运行中，无法取消")
    return {"ok": True}


@app.get("/api/reports")
def report_history():
    """历史报告列表（知识库登记且文件仍存在的）。"""
    return {"reports": knowledge.list_history()}


@app.get("/api/reports/download")
def download_report(name: str):
    """按文件名下载报告。路径先 resolve 再校验父目录，防止 ../ 目录穿越。"""
    path = (REPORT_DIR / name).resolve()
    if path.parent != REPORT_DIR.resolve() or path.suffix != ".md" or not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    return FileResponse(path, filename=path.name, media_type="text/markdown")
