"""S1 持久任务与 S5 版本账本的离线验收。

不联网、不调 LLM、不启浏览器。时间、租约与外部调用都用替身或显式注入。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core.storage.reports import (ReportError, SQLiteReportStore, VersionConflict,
                                  quality_status)
from core.storage.tasks import (CANCELLED, FAILED, NEEDS_REVIEW, QUEUED, RETRY_WAIT,
                                RUNNING, SUCCEEDED, IdempotencyConflict, QueueFull,
                                SQLiteTaskStore, TaskNotFound)
from service.worker import (JobCancelled, JobContext, NeedsReview, RetryableError,
                            TaskWorker)


def _job_payload(key="fixture"):
    return {"keyword": key, "mode": "fast", "force": False}


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteTaskStore(Path(self.tmp.name) / "tasks.db")

    def test_idempotency_same_payload_reuses_and_conflict_409(self):
        a = self.store.submit(owner_id="u1", idempotency_key="k1", kind="research",
                              queue_name="pipeline", payload={"keyword": "甲"})
        b = self.store.submit(owner_id="u1", idempotency_key="k1", kind="research",
                              queue_name="pipeline", payload={"keyword": "甲"})
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["reused"])
        with self.assertRaises(IdempotencyConflict):
            self.store.submit(owner_id="u1", idempotency_key="k1", kind="research",
                              queue_name="pipeline", payload={"keyword": "乙"})
        # 不同主体可以用同一个键；归属隔离
        c = self.store.submit(owner_id="u2", idempotency_key="k1", kind="research",
                              queue_name="pipeline", payload={"keyword": "乙"})
        self.assertNotEqual(a["id"], c["id"])

    def test_queue_and_owner_capacity_are_atomic_limits(self):
        with mock.patch("core.storage.tasks.MAX_QUEUE_TOTAL", 1), \
             mock.patch("core.storage.tasks.MAX_ACTIVE_PER_OWNER", 2):
            self.store.submit(owner_id="u1", idempotency_key="a", kind="research",
                              queue_name="pipeline", payload={"keyword": "甲"})
            with self.assertRaises(QueueFull):
                self.store.submit(owner_id="u2", idempotency_key="b", kind="research",
                                  queue_name="pipeline", payload={"keyword": "乙"})
        with mock.patch("core.storage.tasks.MAX_ACTIVE_PER_OWNER", 1):
            with self.assertRaises(QueueFull):
                self.store.submit(owner_id="u1", idempotency_key="c", kind="research",
                                  queue_name="pipeline", payload={"keyword": "丙"})

    def test_two_consumers_claim_one_job_only_once(self):
        self.store.submit(owner_id="u1", idempotency_key="a", kind="research",
                          queue_name="pipeline", payload=_job_payload())
        barrier = threading.Barrier(2)
        got = []

        def consume(worker):
            barrier.wait()
            got.append(self.store.claim("pipeline", worker, 30))

        threads = [threading.Thread(target=consume, args=(f"w{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for j in got if j), 1)
        self.assertEqual(self.store.stats()["by_queue"]["pipeline"][RUNNING], 1)

    def test_expired_owner_cannot_write_and_retry_gets_new_generation(self):
        job = self.store.submit(owner_id="u1", idempotency_key="a", kind="research",
                                queue_name="pipeline", payload=_job_payload())
        claimed = self.store.claim("pipeline", "w1", 0.01)
        time.sleep(0.03)
        self.assertEqual(self.store.recover_expired(), 1)
        stale = self.store.get(job["id"])
        self.assertEqual(stale["state"], NEEDS_REVIEW)
        self.assertFalse(self.store.complete(job["id"], "w1", claimed["generation"],
                                             state=SUCCEEDED, result={"stale": True}))
        retried = self.store.retry(job["id"], "u1")
        self.assertEqual(retried["state"], RETRY_WAIT)
        claimed2 = self.store.claim("pipeline", "w2", 30)
        self.assertEqual(claimed2["generation"], claimed["generation"] + 1)
        self.assertTrue(self.store.complete(job["id"], "w2", claimed2["generation"],
                                            state=SUCCEEDED, result={"ok": True}))

    def test_cancel_queued_and_running(self):
        q = self.store.submit(owner_id="u1", idempotency_key="q", kind="research",
                              queue_name="pipeline", payload=_job_payload("q"))
        out = self.store.cancel(q["id"], "u1")
        self.assertEqual(out["state"], CANCELLED)
        self.assertIsNone(self.store.claim("pipeline", "w", 30))

        r = self.store.submit(owner_id="u1", idempotency_key="r", kind="research",
                              queue_name="pipeline", payload=_job_payload("r"))
        claimed = self.store.claim("pipeline", "w", 30)
        self.store.cancel(r["id"], "u1")
        self.assertTrue(self.store.complete(r["id"], "w", claimed["generation"],
                                            state=SUCCEEDED, result={"late": True}))
        self.assertEqual(self.store.get(r["id"])["state"], CANCELLED)

    def test_owner_isolation_and_event_cursor(self):
        a = self.store.submit(owner_id="u1", idempotency_key="a", kind="research",
                              queue_name="pipeline", payload=_job_payload())
        self.store.append_event(a["id"], "note", {"n": 1})
        self.store.append_event(a["id"], "note", {"n": 2})
        events = self.store.events(a["id"], after=0, owner_id="u1")
        notes = [e for e in events if e["event_type"] == "note"]
        self.assertEqual([e["payload"]["n"] for e in notes], [1, 2])
        cursor = notes[-2]["event_id"]
        after = self.store.events(a["id"], after=cursor, owner_id="u1")
        self.assertEqual(len(after), 1)
        with self.assertRaises(TaskNotFound):
            self.store.get(a["id"], "u2")
        with self.assertRaises(TaskNotFound):
            self.store.events(a["id"], owner_id="u2")

    def test_cancel_needs_review_is_terminal(self):
        job = self.store.submit(owner_id="u1", idempotency_key="nr", kind="research",
                                queue_name="pipeline", payload=_job_payload())
        self.store.claim("pipeline", "w1", 0.01)
        time.sleep(0.03)
        self.store.recover_expired()
        self.assertEqual(self.store.get(job["id"])["state"], NEEDS_REVIEW)
        out = self.store.cancel(job["id"], "u1")
        self.assertEqual(out["state"], CANCELLED)
        self.assertIsNotNone(out["finished_at"])

    def test_progress_returns_cancel_flag(self):
        self.store.submit(owner_id="u1", idempotency_key="a", kind="research",
                          queue_name="pipeline", payload=_job_payload())
        claimed = self.store.claim("pipeline", "w1", 30)
        self.store.cancel(claimed["id"], "u1")
        r = self.store.progress(claimed["id"], "w1", claimed["generation"])
        self.assertTrue(r["cancel_requested"])
        self.assertEqual(r["state"], "cancel_requested")


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteTaskStore(Path(self.tmp.name) / "tasks.db")

    def _submit(self, key="a"):
        return self.store.submit(owner_id="u1", idempotency_key=key, kind="research",
                                 queue_name="pipeline", payload=_job_payload(key))

    def test_success_and_events(self):
        job = self._submit()

        def handler(job, ctx):
            ctx.attach_legacy({"stage": "工作中", "log": ["第一行"], "cancel_requested": False})
            ctx.sync_progress()
            return {"report_name": "", "value": 1}

        worker = TaskWorker(self.store, queue_name="pipeline", capacity=1, handler=handler,
                            worker_id="w-test")
        self.assertEqual(worker.run_once(), 1)
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], SUCCEEDED)
        self.assertEqual(row["result_json"]["value"], 1)
        types = [e["event_type"] for e in self.store.events(job["id"])]
        self.assertIn("job_claimed", types)
        self.assertIn("stage_changed", types)
        self.assertIn("log", types)
        self.assertIn("job_succeeded", types)

    def test_unknown_external_state_goes_needs_review_not_retry(self):
        job = self._submit()

        def handler(job, ctx):
            raise NeedsReview("响应丢失，结果未知")

        worker = TaskWorker(self.store, queue_name="pipeline", capacity=1, handler=handler,
                            worker_id="w-test")
        worker.run_once()
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], NEEDS_REVIEW)
        self.assertEqual(row["error_code"], "needs_review")
        self.assertEqual(row["attempt"], 1)

    def test_retry_then_exhaust(self):
        job = self._submit()
        calls = []

        def handler(job, ctx):
            calls.append(1)
            raise RetryableError("短暂不可用")

        worker = TaskWorker(self.store, queue_name="pipeline", capacity=1, handler=handler,
                            worker_id="w-test")
        worker.run_once()
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], RETRY_WAIT)
        # 让 available_at 立即就绪，第二次领取后重试耗尽
        db = sqlite3.connect(self.store.path)
        try:
            db.execute("UPDATE task_jobs SET available_at=0 WHERE id=?", (job["id"],))
            db.commit()
        finally:
            db.close()
        worker.run_once()
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], FAILED)
        self.assertEqual(row["error_code"], "retry_exhausted")
        self.assertEqual(len(calls), 2)

    def test_cooperative_cancel(self):
        job = self._submit()
        seen = threading.Event()

        def handler(job, ctx):
            ctx.attach_legacy({"stage": "循环中", "log": [], "cancel_requested": False})
            deadline = time.time() + 3
            while time.time() < deadline:
                ctx.sync_progress()
                if ctx.cancelled():
                    seen.set()
                    raise JobCancelled()
                time.sleep(0.02)
            return {"late": True}

        worker = TaskWorker(self.store, queue_name="pipeline", capacity=1, handler=handler,
                            worker_id="w-test")
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        for _ in range(100):
            if self.store.get(job["id"])["state"] == RUNNING:
                break
            time.sleep(0.01)
        self.store.cancel(job["id"], "u1")
        thread.join(timeout=5)
        self.assertTrue(seen.is_set())
        self.assertEqual(self.store.get(job["id"])["state"], CANCELLED)

    def test_step_is_idempotent_for_same_input(self):
        job = self._submit()
        counter = []

        def handler(job, ctx):
            a = ctx.run_step("report", lambda: counter.append(1) or {"n": 1}, input_hash="x")
            b = ctx.run_step("report", lambda: counter.append(1) or {"n": 2}, input_hash="x")
            self.assertEqual(a, b)
            return {"value": a}

        worker = TaskWorker(self.store, queue_name="pipeline", capacity=1, handler=handler,
                            worker_id="w-test")
        worker.run_once()
        self.assertEqual(len(counter), 1)
        self.assertEqual(self.store.get(job["id"])["state"], SUCCEEDED)

    def test_singleton_blocks_second_pipeline_worker(self):
        w1 = TaskWorker(self.store, queue_name="pipeline", handler=lambda j, c: {},
                        worker_id="w1", singleton=True)
        w1._singleton_id = self.store.acquire_singleton(
            "worker:pipeline", "w1", 30)
        self.assertIsNotNone(w1._singleton_id)
        w2 = TaskWorker(self.store, queue_name="pipeline", handler=lambda j, c: {},
                        worker_id="w2", singleton=True)
        with self.assertRaises(RuntimeError):
            w2.run_once()
        self.store.release_singleton(w1._singleton_id)


def _trip_result(name="行程_甲_1.md", spot="甲园", drop=False):
    return {
        "report_name": name, "markdown": "# 报告",
        "quality": {"passed": not drop, "unresolved": ["[R1] 测试提醒"] if drop else []},
        "trip_plan": {
            "meta": {"city": "甲城", "days": 1, "facts_cutoff": "2026-10-01",
                     "generated_at": "2026-10-01T10:00:00"},
            "itinerary": [{"day": 1, "blocks": [{"slot": "上午", "spot": spot}]}],
            "catalog": {"poi": {spot: {"ticket_price": 10,
                                       "official": {"source_url": "https://official.test/1"}}},
                        "food": {}},
            "snap": {
                "pitfall": [{"claim": "需预约", "quote": "必须提前预约",
                             "source": "https://ugc.test/1", "support_status": "supported",
                             "evidence_id": "ev-1", "confidence": "多源一致"}],
                "digests": {"甲园": {"quote_ids": [], "quote_points": []}},
            },
        },
    }


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def setex(self, key, ttl, value):
        with self.lock:
            self.data[key] = value
        return True

    def delete(self, key):
        with self.lock:
            self.data.pop(key, None)

    def pipeline(self):
        return _FakePipeline(self)

    def ping(self):
        return True


class _FakePipeline:
    def __init__(self, parent):
        self.parent = parent
        self.result = None

    def incr(self, key):
        with self.parent.lock:
            self.parent.data[key] = str(int(self.parent.data.get(key, "0")) + 1)
            self.result = int(self.parent.data[key])
        return self

    def expire(self, key, ttl, nx=False):
        return self

    def execute(self):
        return [self.result]


class _BrokenRedis:
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


class CacheAndLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteTaskStore(Path(self.tmp.name) / "tasks.db")

    def test_key_conditions_are_not_ambiguous(self):
        from core import cache
        self.assertNotEqual(cache.route_key("a", "b", "transit", "b1"),
                            cache.route_key("a", "b", "driving", "b1"))
        self.assertNotEqual(cache.route_key("a", "b", "transit", "b1"),
                            cache.route_key("a", "b", "transit", "b2"))
        self.assertNotEqual(cache.context_key("alice", "r1", "q"),
                            cache.context_key("bob", "r1", "q"))
        self.assertNotEqual(cache.context_key("alice", "r1", "q"),
                            cache.context_key("alice", "r2", "q"))
        self.assertNotEqual(cache.context_key("alice", "r1", "q"),
                            cache.context_key("alice", "r1", "q2"))

    def test_redis_cache_roundtrip_and_negative(self):
        from core.cache import RedisCache
        r = RedisCache("redis://unused")
        r._client = _FakeRedis()
        r.set("k", {"a": 1}, 60)
        self.assertEqual(r.get("k"), {"a": 1})
        calls = []
        v, hit = r.get_or_compute("missing", 60, lambda: calls.append(1) or None,
                                  negative_ttl=5)
        self.assertIsNone(v)
        self.assertFalse(hit)
        v2, hit2 = r.get_or_compute("missing", 60, lambda: calls.append(1) or {"x": 1})
        self.assertIsNone(v2)
        self.assertTrue(hit2)
        self.assertEqual(len(calls), 1)

    def test_redis_failure_falls_back_without_raising(self):
        from core.cache import RedisCache
        r = RedisCache("redis://unused")
        r._client = _BrokenRedis()
        self.assertIsNone(r.get("k"))
        self.assertFalse(r.set("k", 1, 10))
        v, hit = r.get_or_compute("k", 10, lambda: {"ok": True})
        self.assertEqual(v, {"ok": True})
        self.assertFalse(hit)
        self.assertFalse(r.incr_window("rl", 1, 10)["backend"] == "redis")

    def test_single_flight_merges_concurrent_misses(self):
        from core.cache import RedisCache
        r = RedisCache("redis://unused")
        r._client = _FakeRedis()
        calls = []
        barrier = threading.Barrier(10)

        def loader():
            calls.append(1)
            time.sleep(0.05)
            return {"n": 1}

        def worker():
            barrier.wait()
            r.get_or_compute("same-key", 60, loader)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)

    def test_persistent_slots_are_shared_across_store_instances(self):
        other = SQLiteTaskStore(self.store.path)
        lease1 = self.store.acquire_slot("model:global", "u", "j1", 2, 30)
        lease2 = other.acquire_slot("model:global", "u", "j2", 2, 30)
        self.assertIsNotNone(lease1)
        self.assertIsNotNone(lease2)
        self.assertIsNone(self.store.acquire_slot("model:global", "u", "j3", 2, 30))
        self.assertTrue(self.store.release_slot(lease1))
        self.assertFalse(self.store.release_slot(lease1))   # 重复释放不重复生效
        self.assertIsNotNone(other.acquire_slot("model:global", "u", "j3", 2, 30))

    def test_real_redis_client_failure_is_bounded(self):
        from core.cache import RedisCache
        r = RedisCache("redis://127.0.0.1:6399/0")
        started = time.time()
        self.assertIsNone(r.get("missing"))
        self.assertLess(time.time() - started, 3)
        self.assertFalse(r.ping()["ok"])


class ChangeAndDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = SQLiteReportStore(Path(self.tmp.name) / "reports.db")
        self.v1 = self.reports.register_result(
            job_id="j1", owner_id="alice", result=_trip_result("r1.md", "甲园"),
            request={"city": "甲城", "days": 1, "hotel": "", "spots": None,
                     "preferences": "", "preference_mode": "均衡",
                     "start_date": None, "excluded_spots": []})
        result2 = _trip_result("r2.md", "乙馆")
        # 让新版只有乙馆，旧版只有甲园：便于断言增删。
        self.v2 = self.reports.register_result(
            job_id="j2", owner_id="alice", result=result2,
            report_id=self.v1["report_id"], parent_version_id=self.v1["version_id"],
            change_summary="改去乙馆", activate=False,
            request={"city": "甲城", "days": 1, "hotel": "", "spots": None,
                     "preferences": "", "preference_mode": "均衡",
                     "start_date": None, "excluded_spots": ["甲园"]})

    def test_merge_changes_keeps_exclusions_and_summary(self):
        from service.report_versions import ChangeError, merge_changes
        base = {"city": "甲城", "days": 1, "hotel": "", "spots": None,
                "preferences": "老人", "preference_mode": "均衡",
                "start_date": None, "excluded_spots": ["乙馆"]}
        merged, summary = merge_changes(base, {"excluded_spots": ["丙塔"],
                                               "days": 2, "note": "雨天"})
        self.assertEqual(set(merged["excluded_spots"]), {"乙馆", "丙塔"})
        self.assertEqual(merged["days"], 2)
        self.assertIn("雨天", merged["preferences"])
        self.assertIn("排除 丙塔", summary)
        with self.assertRaises(ChangeError):
            merge_changes(base, {"city": "乙城"})
        with self.assertRaises(ChangeError):
            merge_changes(base, {"nonsense": 1})

    def test_diff_rechecks_old_plan_under_new_constraints(self):
        from service.report_versions import diff_versions
        d = diff_versions(self.v1, self.v2)
        self.assertEqual(d["added_spots"], ["乙馆"])
        self.assertEqual(d["removed_spots"], ["甲园"])
        self.assertFalse(d["recheck"]["from_under_new_constraints"]["valid"])
        self.assertIn("甲园", "".join(
            d["recheck"]["from_under_new_constraints"]["issues"]))
        self.assertTrue(d["recheck"]["to_under_new_constraints"]["valid"])

    def test_submit_change_builds_new_request_and_task(self):
        from unittest import mock
        from service import report_versions
        captured = {}

        def fake_submit(kind, payload, **kwargs):
            captured.update({"kind": kind, "payload": payload, **kwargs})
            return {"id": "job-change", "state": "queued", "legacy_status": "running",
                    "request_id": "r", "trace_id": "t", "reused": False}

        with mock.patch.object(report_versions, "report_store", return_value=self.reports), \
             mock.patch.object(report_versions.task_service, "submit", side_effect=fake_submit):
            out = report_versions.submit_change(
                owner_id="alice", report_id=self.v1["report_id"],
                base_version_id=self.v1["version_id"],
                changes={"excluded_spots": ["乙馆"], "note": "雨天改室内"},
                idempotency_key="change-1")
        self.assertEqual(captured["kind"], "trip_change")
        self.assertEqual(captured["parent_version_id"], self.v1["version_id"])
        self.assertEqual(captured["base_report_id"], self.v1["report_id"])
        self.assertIn("乙馆", captured["payload"]["excluded_spots"])
        self.assertEqual(out["base_version_id"], self.v1["version_id"])


class VersionApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = SQLiteReportStore(Path(self.tmp.name) / "reports.db")
        self.v1 = self.reports.register_result(
            job_id="j1", owner_id="local_single_user", result=_trip_result("r1.md", "甲园"),
            request={"city": "甲城", "days": 1, "hotel": "", "spots": None,
                     "preferences": "", "preference_mode": "均衡",
                     "start_date": None, "excluded_spots": []})
        self.v2 = self.reports.register_result(
            job_id="j2", owner_id="local_single_user",
            result=_trip_result("r2.md", "乙馆"),
            report_id=self.v1["report_id"], parent_version_id=self.v1["version_id"],
            change_summary="改去乙馆", activate=False,
            request={"city": "甲城", "days": 1, "hotel": "", "spots": None,
                     "preferences": "", "preference_mode": "均衡",
                     "start_date": None, "excluded_spots": ["甲园"]})

    def _client(self):
        from fastapi.testclient import TestClient
        from unittest import mock
        from service import report_versions
        import api_server
        patcher = mock.patch.object(report_versions, "report_store",
                                    return_value=self.reports)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TestClient(api_server.app)

    def test_versions_diff_and_activate_cas(self):
        c = self._client()
        r = c.get(f"/api/reports/{self.v1['report_id']}/versions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["versions"]), 2)
        d = c.get(f"/api/reports/{self.v1['report_id']}/diff",
                  params={"to_version_id": self.v2["version_id"]})
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["added_spots"], ["乙馆"])
        bad = c.post(f"/api/reports/{self.v1['report_id']}/activate",
                     json={"version_id": self.v2["version_id"],
                           "expected_active_version_id": "stale"})
        self.assertEqual(bad.status_code, 409)
        ok = c.post(f"/api/reports/{self.v1['report_id']}/activate",
                    json={"version_id": self.v2["version_id"],
                          "expected_active_version_id": self.v1["version_id"]})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["active_version_id"], self.v2["version_id"])

    def test_change_api_returns_accepted_job(self):
        from unittest import mock
        from service import report_versions
        captured = {}

        def fake_submit(*args, **kwargs):
            captured.update(kwargs)
            return {"id": "change-job", "state": "queued", "legacy_status": "running",
                    "request_id": "r", "trace_id": "t", "reused": False,
                    "base_version_id": kwargs.get("parent_version_id"),
                    "change_summary": "排除 乙馆"}

        c = self._client()
        with mock.patch.object(report_versions.task_service, "submit",
                               side_effect=fake_submit):
            r = c.post(f"/api/reports/{self.v1['report_id']}/changes",
                       json={"base_version_id": self.v1["version_id"],
                             "changes": {"excluded_spots": ["乙馆"]}},
                       headers={"Idempotency-Key": "k-change"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["job_id"], "change-job")
        self.assertEqual(captured["parent_version_id"], self.v1["version_id"])
        self.assertEqual(captured["base_report_id"], self.v1["report_id"])


class ChangeWorkerIntegrationTest(unittest.TestCase):
    def test_trip_change_worker_registers_child_version(self):
        from unittest import mock
        from service import research, task_service, trip
        from service.research import JOBS
        from service.worker import TaskWorker

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SQLiteTaskStore(Path(tmp.name) / "tasks.db")
        reports = SQLiteReportStore(Path(tmp.name) / "reports.db")
        base = reports.register_result(
            job_id="base-job", owner_id="local_single_user",
            result=_trip_result("base.md", "甲园"),
            request={"city": "甲城", "days": 1, "hotel": "", "spots": None,
                     "preferences": "", "preference_mode": "均衡",
                     "start_date": None, "excluded_spots": []})
        out = store.submit(
            owner_id="local_single_user", idempotency_key="change",
            kind="trip_change", queue_name="pipeline",
            payload={"city": "甲城", "days": 1, "hotel": "", "spots": None,
                     "preferences": "雨天", "preference_mode": "均衡",
                     "start_date": None, "excluded_spots": ["乙馆"],
                     "report_id": base["report_id"], "base_version_id": base["version_id"],
                     "change_summary": "排除乙馆", "changes": {"excluded_spots": ["乙馆"]}},
            parent_version_id=base["version_id"], base_report_id=base["report_id"])

        def fake_run_trip(job_id, city, days, hotel, spots, preferences,
                          preference_mode, start_date, excluded_spots):
            legacy = JOBS[job_id]
            legacy["stage"] = "完成"
            legacy["status"] = "done"
            legacy["result"] = _trip_result("changed.md", "丙塔")

        worker = TaskWorker(store, queue_name="pipeline", capacity=1, worker_id="w")
        with mock.patch.object(trip, "_run_trip", side_effect=fake_run_trip), \
             mock.patch.object(task_service, "_reports", return_value=reports):
            worker.run_once()
        row = store.get(out["id"])
        self.assertEqual(row["state"], SUCCEEDED)
        version = reports.find_version_by_job(out["id"])
        self.assertIsNotNone(version)
        self.assertEqual(version["report_id"], base["report_id"])
        self.assertEqual(version["parent_version_id"], base["version_id"])
        self.assertEqual(version["version_no"], 2)
        report = reports.get_report(base["report_id"], "local_single_user")
        self.assertEqual(report["active_version_id"], base["version_id"])  # 未自动激活


class AuthAndMySQLContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteTaskStore(Path(self.tmp.name) / "tasks.db")

    def test_token_authentication_and_owner_isolation(self):
        import core.auth as auth
        from core.auth import AuthError, authenticate, create_principal
        create_principal("alice", "tok-alice", store=self.store)
        create_principal("bob", "tok-bob", store=self.store)
        with mock.patch.object(auth, "AUTH_MODE", "api_token"):
            self.assertEqual(authenticate("Bearer tok-alice", store=self.store).id, "alice")
            self.assertEqual(authenticate("Bearer tok-bob", store=self.store).id, "bob")
            with self.assertRaises(AuthError):
                authenticate("Bearer wrong", store=self.store)
            with self.assertRaises(AuthError):
                authenticate(None, store=self.store)
        job = self.store.submit(owner_id="alice", idempotency_key="x", kind="research",
                                queue_name="pipeline", payload={"keyword": "甲"})
        with self.assertRaises(TaskNotFound):
            self.store.get(job["id"], "bob")

    def test_mysql_backend_without_dsn_fails_loudly(self):
        from core.storage import mysql_tasks
        with mock.patch.object(mysql_tasks, "MYSQL_DSN", ""):
            with self.assertRaises(RuntimeError):
                mysql_tasks.MySQLTaskStore()
        # 有 DSN 时只创建 engine，不在此处联网（真实集成需服务器）。
        engine = mysql_tasks._engine("mysql+pymysql://u:p@127.0.0.1:3306/travelguide")
        self.assertIn("mysql", str(engine.url))

    def test_mysql_dialect_compiles_claim_and_capacity_sql(self):
        from sqlalchemy import text
        from sqlalchemy.dialects import mysql
        claim = text(
            "SELECT * FROM task_jobs WHERE queue_name=:q AND state IN ('queued','retry_wait')"
            " AND available_at<=:now AND cancel_requested=0"
            " ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED")
        compiled = str(claim.compile(dialect=mysql.dialect(),
                                     compile_kwargs={"literal_binds": True}))
        self.assertIn("FOR UPDATE SKIP LOCKED", compiled)
        self.assertIn("LIMIT 1", compiled)


class ReportVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteReportStore(Path(self.tmp.name) / "reports.db")

    def test_initial_version_active_and_evidence_bound(self):
        v1 = self.store.register_result(job_id="j1", owner_id="u1", result=_trip_result())
        self.assertEqual(v1["version_no"], 1)
        report = self.store.get_report(v1["report_id"], "u1")
        self.assertEqual(report["active_version_id"], v1["version_id"])
        self.assertEqual(v1["quality_status"], "pass")
        ids = {e["evidence_id"] for e in v1["evidence"]}
        self.assertIn("ev-1", ids)

    def test_change_version_inactive_until_cas_activate(self):
        v1 = self.store.register_result(job_id="j1", owner_id="u1", result=_trip_result())
        v2 = self.store.register_result(
            job_id="j2", owner_id="u1", result=_trip_result("行程_甲_2.md", "乙馆"),
            report_id=v1["report_id"], parent_version_id=v1["version_id"],
            change_summary="雨天改室内")
        self.assertEqual(v2["version_no"], 2)
        report = self.store.get_report(v1["report_id"], "u1")
        self.assertEqual(report["active_version_id"], v1["version_id"])   # 未自动激活
        with self.assertRaises(VersionConflict):
            self.store.activate(v1["report_id"], v2["version_id"], "wrong", "u1")
        report = self.store.activate(v1["report_id"], v2["version_id"],
                                     v1["version_id"], "u1")
        self.assertEqual(report["active_version_id"], v2["version_id"])
        self.assertEqual(self.store.get_version(v1["version_id"])["version_no"], 1)
        self.assertEqual(self.store.get_version(v1["version_id"])["content_hash"],
                         v1["content_hash"])

    def test_quality_warning_is_not_passed(self):
        self.assertEqual(quality_status(_trip_result(drop=True)), "warning")
        self.assertEqual(quality_status(_trip_result()), "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)