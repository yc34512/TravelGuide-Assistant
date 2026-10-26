"""故障注入验收：直接检查任务/缓存故障窗口，并运行既有关键恢复用例。

不访问真实平台、不调用真实模型。输出只证明本地故障处理逻辑，
不宣称跨主机或远端 exactly-once。
"""
from __future__ import annotations

import json
import platform
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from core.cache import RedisCache
from core.storage.tasks import (CANCELLED, NEEDS_REVIEW, RETRY_WAIT, RUNNING,
                                SUCCEEDED, SQLiteTaskStore)
from service.worker import NeedsReview, RetryableError, TaskWorker


class BrokenRedis:
    def get(self, *a, **k):
        raise ConnectionError("down")

    def setex(self, *a, **k):
        raise ConnectionError("down")

    def delete(self, *a, **k):
        raise ConnectionError("down")

    def pipeline(self):
        return self

    def incr(self, *a, **k):
        raise ConnectionError("down")

    def expire(self, *a, **k):
        return True

    def execute(self):
        raise ConnectionError("down")

    def ping(self):
        raise ConnectionError("down")


def _store(tmp):
    return SQLiteTaskStore(Path(tmp) / "tasks.db")


def check_double_claim(tmp):
    store = _store(tmp)
    store.submit(owner_id="u", idempotency_key="a", kind="research",
                 queue_name="pipeline", payload={"keyword": "x"})
    barrier = threading.Barrier(2)
    got = []

    def run(w):
        barrier.wait()
        got.append(store.claim("pipeline", w, 30))

    ts = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for x in got if x) == 1


def check_stale_write(tmp):
    store = _store(tmp)
    job = store.submit(owner_id="u", idempotency_key="a", kind="research",
                       queue_name="pipeline", payload={"keyword": "x"})
    claimed = store.claim("pipeline", "w1", 0.01)
    time.sleep(0.03)
    assert store.recover_expired() == 1
    assert store.get(job["id"])["state"] == NEEDS_REVIEW
    assert not store.complete(job["id"], "w1", claimed["generation"],
                              state=SUCCEEDED, result={"stale": True})


def check_cancel_running(tmp):
    store = _store(tmp)
    job = store.submit(owner_id="u", idempotency_key="a", kind="research",
                       queue_name="pipeline", payload={"keyword": "x"})
    claimed = store.claim("pipeline", "w1", 30)
    store.cancel(job["id"], "u")
    assert store.progress(job["id"], "w1", claimed["generation"])["cancel_requested"]
    assert store.complete(job["id"], "w1", claimed["generation"],
                          state=SUCCEEDED, result={})
    assert store.get(job["id"])["state"] == CANCELLED


def check_redis_disconnect(tmp):
    r = RedisCache("redis://unused")
    r._client = BrokenRedis()
    assert r.get("k") is None
    assert r.set("k", 1, 10) is False
    value, hit = r.get_or_compute("k", 10, lambda: {"fallback": True})
    assert value == {"fallback": True} and hit is False


def check_unknown_external(tmp):
    store = _store(tmp)
    job = store.submit(owner_id="u", idempotency_key="a", kind="research",
                       queue_name="pipeline", payload={"keyword": "x"})

    def handler(j, ctx):
        raise NeedsReview("结果未知")

    worker = TaskWorker(store, queue_name="pipeline", capacity=1, handler=handler,
                        worker_id="w")
    worker.run_once()
    assert store.get(job["id"])["state"] == NEEDS_REVIEW


def check_retry_exhaustion(tmp):
    store = _store(tmp)
    job = store.submit(owner_id="u", idempotency_key="a", kind="research",
                       queue_name="pipeline", payload={"keyword": "x"},
                       max_attempts=2)

    def handler(j, ctx):
        raise RetryableError("短暂失败")

    worker = TaskWorker(store, queue_name="pipeline", capacity=1, handler=handler,
                        worker_id="w")
    with mock.patch("service.worker.WORKER_RETRY_DELAY_SECONDS", 0):
        worker.run_once()
        assert store.get(job["id"])["state"] == RETRY_WAIT
        worker.run_once()
    assert store.get(job["id"])["state"] == "failed"


DIRECT = [
    ("double_consumer_single_claim", check_double_claim),
    ("expired_owner_stale_write_rejected", check_stale_write),
    ("cancel_running_is_cooperative", check_cancel_running),
    ("redis_disconnect_bounded_fallback", check_redis_disconnect),
    ("unknown_external_goes_needs_review", check_unknown_external),
    ("retry_exhaustion_terminal_failed", check_retry_exhaustion),
]

UNIT_TESTS = [
    "tests_planning.PlanningTest.test_subprocess_generate_checkpoint_resumes_without_call",
    "tests_planning.PlanningTest.test_subprocess_quality_checkpoint_resumes_without_call",
    "tests_planning.PlanningTest.test_response_saved_before_graph_checkpoint_is_reused",
    "tests_planning.PlanningTest.test_uncertain_request_window_marked_unknown_then_counted_again",
    "tests_planning.PlanningTest.test_budget_not_refilled_after_restart",
    "tests_planning.PlanningTest.test_finalizer_registration_failure_then_idempotent_retry",
    "tests_planning.PlanningTest.test_index_repair_checks_all_keys_not_any_prefix",
    "tests_planning.PlanningTest.test_skipped_embedding_can_be_completed_after_configuration",
    "tests_planning.PlanningTest.test_cancel_after_request_stops_before_next_node",
    "tests_planning.PlanningTest.test_resume_api_reuses_snapshot_and_no_crawl_or_session_reset",
]


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="故障注入验收")
    parser.add_argument("--out", default=str(PROJECT / "data" / "upgrade"
                                             / "fault_injection.json"))
    args = parser.parse_args(argv)
    rows = []
    for name, fn in DIRECT:
        tmp = tempfile.TemporaryDirectory()
        t0 = time.perf_counter()
        try:
            fn(tmp.name)
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            tmp.cleanup()
        rows.append({"name": name, "kind": "direct", "ok": error is None,
                     "error": error, "elapsed_s": round(time.perf_counter() - t0, 3)})
    loader = unittest.TestLoader()
    for name in UNIT_TESTS:
        t0 = time.perf_counter()
        suite = loader.loadTestsFromName(name)
        result = unittest.TestResult()
        suite.run(result)
        err = None
        if result.errors or result.failures:
            err = (result.errors + result.failures)[0][1].strip().splitlines()[-1]
        rows.append({"name": name, "kind": "unit", "ok": result.wasSuccessful(),
                     "error": err, "elapsed_s": round(time.perf_counter() - t0, 3)})
    report = {
        "kind": "fault-injection", "real_services": False,
        "environment": {"python": sys.version.split()[0],
                        "platform": platform.platform()},
        "results": rows,
        "summary": {"total": len(rows), "passed": sum(1 for r in rows if r["ok"]),
                    "failed": sum(1 for r in rows if not r["ok"])},
        "limitations": [
            "未在任意指令点强杀进程；子进程用例覆盖 checkpoint 前后中断。",
            "不宣称远端 LLM/地图 exactly-once；未知结果保留为 unknown/needs_review。",
            "真实 MySQL/Redis 服务器未启动，相关集成项未在此脚本验证。",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print(json.dumps(report["summary"], ensure_ascii=False))
    for r in rows:
        if not r["ok"]:
            print("FAILED", r["name"], r["error"])
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())