"""本地容量测试：提交/查询容量与“完整生成”替身容量分开报告。

- 提交/查询：真实 SQLite 事务、鉴权、队列、拒绝；外部服务全部是本地替身。
- 完整生成：pipeline 容量 1、query 容量 2 的本地 mock handler；这不是 LLM/浏览器吞吐。
所有数字只对应本机与本次配置，不构成线上 QPS 或“支持多少人同时生成”的结论。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import numpy as np

from core.auth import authenticate, create_principal
from core.storage.tasks import SQLiteTaskStore
from service.worker import TaskWorker


def _pct(values, q):
    return round(float(np.percentile(values, q)), 3) if values else None


def submit_scenario(level: int) -> dict:
    import core.auth as auth
    auth.AUTH_MODE = "api_token"
    tmp = tempfile.TemporaryDirectory()
    store = SQLiteTaskStore(Path(tmp.name) / "tasks.db")
    for i in range(level):
        create_principal(f"u{i}", f"tok{i}", store=store)
    results, latencies = [], []
    barrier = threading.Barrier(level)
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        t0 = time.perf_counter()
        try:
            principal = authenticate(f"Bearer tok{i}", store=store)
            out = store.submit(owner_id=principal.id, idempotency_key=f"k{i}",
                               kind="research", queue_name="pipeline",
                               payload={"keyword": f"q{i}"})
            err = None
        except Exception as exc:
            out, err = None, type(exc).__name__
        dt = (time.perf_counter() - t0) * 1000
        with lock:
            latencies.append(dt)
            results.append({"ok": bool(out), "error": err,
                            "wait": out is not None})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(level)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    stats = store.stats()
    tmp.cleanup()
    return {"concurrency": level, "elapsed_s": round(elapsed, 3),
            "accepted": sum(1 for r in results if r["ok"]),
            "rejected": sum(1 for r in results if not r["ok"]),
            "errors": sorted({r["error"] for r in results if r["error"]}),
            "latency_ms": {"p50": _pct(latencies, 50), "p95": _pct(latencies, 95),
                           "max": round(max(latencies), 3) if latencies else None},
            "queue_states": stats["by_queue"]}


def rejection_scenario() -> dict:
    from unittest import mock
    tmp = tempfile.TemporaryDirectory()
    store = SQLiteTaskStore(Path(tmp.name) / "tasks.db")
    accepted = rejected = 0
    with mock.patch("core.storage.tasks.MAX_QUEUE_TOTAL", 5), \
         mock.patch("core.storage.tasks.MAX_ACTIVE_PER_OWNER", 1):
        for i in range(10):
            try:
                store.submit(owner_id=f"u{i}", idempotency_key=f"k{i}",
                             kind="research", queue_name="pipeline",
                             payload={"keyword": "x"})
                accepted += 1
            except Exception as exc:
                rejected += 1
    tmp.cleanup()
    return {"configured_queue_cap": 5, "accepted": accepted, "rejected": rejected,
            "note": "拒绝来自持久队列容量，不是模拟的网络错误"}


def generation_scenario(pipeline_n=8, query_n=8, handler_sleep=0.02) -> dict:
    tmp = tempfile.TemporaryDirectory()
    store = SQLiteTaskStore(Path(tmp.name) / "tasks.db")
    jobs = {}
    for i in range(pipeline_n):
        out = store.submit(owner_id=f"p{i}", idempotency_key=f"p{i}", kind="research",
                           queue_name="pipeline", payload={"keyword": "mock"})
        jobs[out["id"]] = "pipeline"
    for i in range(query_n):
        out = store.submit(owner_id=f"q{i}", idempotency_key=f"q{i}", kind="agent_ask",
                           queue_name="query", payload={"question": "mock"})
        jobs[out["id"]] = "query"

    def handler(job, ctx):
        time.sleep(handler_sleep)
        return {"mock": True, "report_name": ""}

    pipeline = TaskWorker(store, queue_name="pipeline", capacity=1, handler=handler,
                          worker_id="mock-pipeline", poll_seconds=0.02)
    query = TaskWorker(store, queue_name="query", capacity=2, handler=handler,
                       worker_id="mock-query", poll_seconds=0.02)
    pipeline.start()
    query.start()
    t0 = time.perf_counter()
    deadline = time.time() + 60
    while time.time() < deadline:
        rows = [store.get(jid) for jid in jobs]
        if all(r["state"] in ("succeeded", "failed", "cancelled", "needs_review")
               for r in rows):
            break
        time.sleep(0.02)
    elapsed = time.perf_counter() - t0
    pipeline.stop()
    query.stop()
    waits = []
    for r in rows:
        if r.get("started_at") and r.get("created_at"):
            waits.append((r["started_at"] - r["created_at"]) * 1000)
    out = {"pipeline_jobs": pipeline_n, "query_jobs": query_n,
           "pipeline_capacity": 1, "query_capacity": 2,
           "handler_sleep_ms": handler_sleep * 1000,
           "elapsed_s": round(elapsed, 3),
           "completed": sum(1 for r in rows if r["state"] == "succeeded"),
           "failed": sum(1 for r in rows if r["state"] != "succeeded"),
           "queue_wait_ms": {"p50": _pct(waits, 50), "p95": _pct(waits, 95),
                             "max": round(max(waits), 3) if waits else None},
           "states": {r["state"]: sum(1 for x in rows if x["state"] == r["state"])
                      for r in rows}}
    tmp.cleanup()
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地容量测试（非线上 QPS）")
    parser.add_argument("--out", default=str(PROJECT / "data" / "upgrade"
                                             / "capacity_report.json"))
    parser.add_argument("--levels", default="1,5,10,20,50,100")
    args = parser.parse_args(argv)
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    report = {
        "kind": "local-capacity", "not_online_qps": True,
        "note": ("SQLite + 本地 mock handler；外部服务未调用。"
                 "查询/提交容量与完整生成容量分开，不能写成真实 LLM/浏览器吞吐。"),
        "environment": {
            "python": sys.version.split()[0], "platform": platform.platform(),
            "cpu_count": os.cpu_count(), "pid": os.getpid(),
            "maxrss": None, "maxrss_note": "Windows 未提供 resource.getrusage",
        },
        "submit_query": [submit_scenario(n) for n in levels],
        "rejection": rejection_scenario(),
        "generation_mock": generation_scenario(),
        "cold_cache": True,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print(json.dumps({"submit_query": report["submit_query"],
                      "rejection": report["rejection"],
                      "generation_mock": report["generation_mock"]},
                     ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())