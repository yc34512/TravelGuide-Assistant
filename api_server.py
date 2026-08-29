"""FastAPI 服务：网页界面 + 任务接口。

接口：
    GET  /                 网页界面
    GET  /api/health       健康检查 + 知识库概览
    POST /api/research     发起研究任务 {keyword, limit, comments, force} -> {job_id}
    GET  /api/jobs/{id}    轮询任务状态/进度/结果
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import KB_TTL_DAYS
from core import knowledge
from service import research

app = FastAPI(title="旅游攻略助手", description="抖音 UGC 采集 + AI 攻略整合，来源可溯")

_WEB_DIR = Path(__file__).parent / "web"


class ResearchIn(BaseModel):
    keyword: str
    limit: int = 10
    comments: int = 50
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
    # 入参收敛：防止单次任务过大
    limit = min(max(body.limit, 3), 20)
    comments = min(max(body.comments, 10), 100)
    job_id = research.start_job(keyword, limit=limit, comments=comments, force=body.force)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = research.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job
