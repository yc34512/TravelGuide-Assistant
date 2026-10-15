"""一期规划的固定反例、真实SDK/mock transport、子进程恢复及业务出口。

所有城市、材料、模型usage都是合成夹具。测试数量不代表旅游准确率。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from core.planning_runs import BudgetExhausted, InvalidPlanError, RunBusy, RunCancelled, RunError, RunStore
from core.model_calls import FatalModelError, request_json
from pipeline.planning_graph import execute
from pipeline.planning_policy import assess_constraints, evaluate, prefer


def sample_snapshot():
    from pipeline.planner import empty_profile
    profiles = {name: empty_profile() | {"duration_hours": 1, "best_time_slot": "任意"}
                for name in ("甲园", "乙馆", "丙塔")}
    return {"city": "合成测试城", "days": 1, "hotel": "", "profiles": profiles,
            "travel_lines": [], "preferences": "", "preference_mode": "均衡",
            "repair_rounds": 1, "constraints": {"excluded_spots": []},
            "cands": [{"name": n, "category": "景点"} for n in profiles],
            "spot_sources": {n: ["https://fixture.test/" + str(i)] for i, n in enumerate(profiles)},
            "spot_points": {}, "food_profiles": {}, "today": "2026-10-14",
            "day_weekdays": ["周一"], "official_map": {}, "pitfall": [],
            "locs": {}, "heat_rows": [], "funnel_info": {}, "funnel_unresolved": [],
            "generated_at": "2026-10-14T12:00:00", "facts_cutoff": "2026-10-14",
            "collect_mode": "synthetic-fixture", "video_count": 0, "comment_count": 0}


def sample_plan(names=("甲园", "乙馆")):
    return {"days": [{"day": 1, "slots": [{"spot": n, "slot": "上午" if i == 0 else "下午",
                                             "food": "模型虚构餐厅"} for i, n in enumerate(names)]}],
            "summary_note": "未排点位列为备选，按时间限制安排"}


class FixedModel:
    """通过真实OpenAI SDK和HTTPX MockTransport验证每个create实际发送次数。"""
    def __init__(self, responses, *, usage=True, callback=None):
        self.responses = list(responses)
        self.requests = []
        self.usage = usage
        self.callback = callback
        self.client = OpenAI(api_key="synthetic-not-a-secret", base_url="https://fixture.test/v1",
                             max_retries=2, http_client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def handle(self, req):
        body = json.loads(req.content)
        self.requests.append(body)
        if self.callback:
            self.callback(len(self.requests), req)
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "synthetic failure", "type": "fixture"}})
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        response = {"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture-model",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}
        if self.usage:
            response["usage"] = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        return httpx.Response(200, json=response)

    def factory(self):
        return self.client, "fixture-model"

    def close(self):
        self.client.close()


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RunStore(self.root / "runs.db")
        self.sleep_patch = patch("core.model_calls.time.sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def model(self, responses, **kwargs):
        m = FixedModel(responses, **kwargs)
        self.addCleanup(m.close)
        return m

    def create(self, snapshot=None, **kwargs):
        return self.store.create(snapshot or sample_snapshot(), **kwargs)

    def run_graph(self, rid, model, **kwargs):
        with self.store.lease(rid):
            return execute(self.store, rid, client_factory=model.factory, **kwargs)

    def test_legacy_migration_matches_prompt_sequence_and_output(self):
        from pipeline import planner
        s = sample_snapshot()
        s["official_map"] = {"甲园": {"close_day": "周一", "valid_until": "2027-01-01"}}
        outputs = [sample_plan(), sample_plan(), sample_plan(("乙馆", "丙塔"))]
        old_requests = []
        queue = deepcopy(outputs)
        def old_call(system, user, **kw):
            old_requests.append({"system": system, "user": user})
            return queue.pop(0)
        def legacy_plan(extra=None):
            return planner.plan_itinerary(s["city"], s["days"], s["hotel"], s["profiles"], [], "",
                                           extra_issues=extra, points_by_spot={})
        with patch.object(planner, "chat_json", side_effect=old_call):
            plan = legacy_plan()
            best = evaluate(plan, s)
            if any(c["status"] == "fail" for c in best["quality"]["checks"]):
                revised = evaluate(legacy_plan(best["quality"]["issues"]), s)
                if prefer(best, revised, "legacy-v1")[0]:
                    best = revised
        rid = self.create(s, policy="legacy-v1")
        m = self.model(outputs)
        result = self.run_graph(rid, m)
        self.assertEqual(result["best"]["plan"], best["plan"])
        prompts = [{"system": q["messages"][0]["content"], "user": q["messages"][1]["content"]} for q in m.requests]
        self.assertEqual(prompts, old_requests)
        self.assertEqual(len(m.requests), 3)
        self.assertTrue(all(not slot["food"] for d in result["best"]["plan"]["days"] for slot in d["slots"]))

    def test_legacy_inner_coverage_repair(self):
        rid = self.create(policy="legacy-v1")
        m = self.model([sample_plan(("甲园",)), sample_plan()])
        result = self.run_graph(rid, m)
        self.assertEqual(len(m.requests), 2)
        self.assertEqual(len(result["best"]["plan"]["days"][0]["slots"]), 2)

    def test_hard_policy_graph_removes_closed_day_conflict(self):
        s = sample_snapshot()
        s["official_map"] = {"甲园": {"close_day": "周一", "valid_until": "2027-01-01"}}
        rid = self.create(s)
        m = self.model([sample_plan(), sample_plan(), sample_plan(("乙馆", "丙塔"))])
        result = self.run_graph(rid, m)
        self.assertEqual(result["best"]["constraints"]["hard_ids"], [])
        self.assertEqual(len(m.requests), 3)
        self.assertTrue(any(e["detail"] == "hard_set_reduced" for e in self.store.events(rid)))

    def test_hard_first_rejects_new_violation_despite_soft_improvement(self):
        old = {"quality": {"checks": [{"status": "fail"}]},
               "constraints": {"hard_ids": [], "evaluated_ids": ["closed:甲园"], "contract_valid": True}}
        new = deepcopy(old)
        new["quality"]["checks"] = [{"status": "pass"}]
        new["constraints"]["hard_ids"] = ["closed:甲园:1"]
        self.assertTrue(prefer(old, new, "legacy-v1")[0])
        self.assertEqual(prefer(old, new, "hard-first-v1"), (False, "new_hard_violation"))

    def test_inner_repair_removes_hard_violation_even_with_more_coverage_issues(self):
        s = sample_snapshot()
        s["constraints"]["excluded_spots"] = ["甲园"]
        s["repair_rounds"] = 0
        old, new = sample_plan(), sample_plan(("乙馆",))
        old["summary_note"] = new["summary_note"] = ""
        rid = self.create(s)
        out = self.run_graph(rid, self.model([old, new]))
        self.assertEqual(out["best"]["constraints"]["hard_ids"], [])
        self.assertEqual(out["best"]["plan"]["days"][0]["slots"][0]["spot"], "乙馆")
        self.assertTrue(any(e["node_id"] == "select_coverage" and e["detail"] == "hard_set_reduced"
                            and e["status"] == "adopt" for e in self.store.events(rid)))

    def test_hard_violation_never_finishes_as_quality_complete(self):
        s = sample_snapshot()
        s["constraints"]["excluded_spots"] = ["甲园"]
        s["repair_rounds"] = 0
        out = self.run_graph(self.create(s), self.model([sample_plan()]))
        self.assertTrue(out["best"]["constraints"]["hard_ids"])
        self.assertFalse(any(c["status"] == "fail" for c in out["best"]["quality"]["checks"]))
        self.assertEqual(out["stop_reason"], "repair_limit")

    def test_invalid_first_candidate_is_diagnostic_not_best_or_report(self):
        s = sample_snapshot()
        s["days"] = 2
        rid = self.create(s)
        m = self.model([sample_plan()] * 4)
        with self.assertRaises(InvalidPlanError):
            self.run_graph(rid, m)
        self.assertEqual(len(m.requests), 4)
        self.assertIsNone(self.store.step(rid, "latest_best"))
        self.assertIsNone(self.store.step(rid, "graph_output"))
        self.assertFalse(self.store.step(rid, "latest_rejected")["constraints"]["contract_valid"])
        self.assertEqual(self.store.get(rid)["status"], "invalid_plan")
        self.assertFalse(self.store.public(rid)["resumable"])

    def test_invalid_initial_candidate_can_be_fixed_in_later_round(self):
        s = sample_snapshot()
        s["days"] = 2
        fixed = sample_plan(("甲园",))
        fixed["days"].append({"day": 2, "slots": [{"spot": "乙馆", "slot": "下午"}]})
        rid = self.create(s)
        out = self.run_graph(rid, self.model([sample_plan(), sample_plan(), fixed, fixed]))
        self.assertTrue(out["best"]["constraints"]["contract_valid"])
        self.assertIsNotNone(self.store.step(rid, "latest_rejected"))

    def test_expired_facts_not_used_as_qc_repair_instruction_in_strict_policy(self):
        s = sample_snapshot()
        s["official_map"] = {"甲园": {"close_day": "周一", "valid_until": "2020-01-01"}}
        old = evaluate(sample_plan(), s, "legacy-v1")
        new = evaluate(sample_plan(), s, "hard-first-v1")
        self.assertEqual(next(c for c in old["quality"]["checks"] if c["rule_id"] == "R3")["status"], "fail")
        self.assertNotEqual(next(c for c in new["quality"]["checks"] if c["rule_id"] == "R3")["status"], "fail")
        self.assertIn("closed:甲园", new["constraints"]["unknown_ids"])
        self.assertNotIn("closed:甲园:1", new["constraints"]["hard_ids"])

    def test_empty_plan_cannot_block_first_valid_plan_with_disclosed_conflict(self):
        s = sample_snapshot()
        s["days"], s["repair_rounds"] = 2, 0
        s["constraints"]["excluded_spots"] = ["甲园"]
        fixed = sample_plan(("甲园",))
        fixed["days"].append({"day": 2, "slots": [{"spot": "乙馆", "slot": "下午"}]})
        rid = self.create(s)
        out = self.run_graph(rid, self.model([{"days": []}, fixed]))
        self.assertTrue(out["best"]["constraints"]["contract_valid"])
        self.assertEqual(out["best"]["constraints"]["hard_ids"], ["excluded:甲园:1"])
        self.assertEqual(out["stop_reason"], "repair_limit")
        issue = out["best"]["constraints"]["issues"][0]
        self.assertEqual(issue["evidence_refs"][0]["path"], ["snapshot", "constraints", "excluded_spots"])
        self.assertEqual(issue["violation_id"], "excluded:甲园:1")
        self.assertTrue(any(e["detail"] == "first_valid_contract" for e in self.store.events(rid)))

    def test_duplicate_slots_and_all_day_overlap_fail_contract(self):
        s = sample_snapshot()
        for when in ("上午", "全天"):
            with self.subTest(when=when):
                plan = sample_plan()
                plan["days"][0]["slots"][1]["slot"] = when
                assessment = assess_constraints(plan, s)
                self.assertFalse(assessment["contract_valid"])
                self.assertIn("contract:plan", assessment["hard_ids"])
        self.assertTrue(assess_constraints(sample_plan(), s)["contract_valid"])

    def test_equal_hard_counts_with_different_ids_are_rejected(self):
        old = {"quality": {"checks": [{"status": "fail"}]},
               "constraints": {"hard_ids": ["closed:A:1"], "evaluated_ids": ["closed:A", "closed:B"], "contract_valid": True}}
        new = deepcopy(old)
        new["constraints"]["hard_ids"] = ["closed:B:1"]
        new["quality"]["checks"] = []
        self.assertFalse(prefer(old, new, "hard-first-v1")[0])

    def test_hard_subset_wins_and_ties_keep_old(self):
        old = {"quality": {"checks": []}, "constraints": {
            "hard_ids": ["closed:A:1"], "evaluated_ids": ["closed:A"], "contract_valid": True}}
        new = deepcopy(old)
        new["constraints"]["hard_ids"] = []
        new["quality"]["checks"] = [{"status": "warn"}] * 5
        self.assertEqual(prefer(old, new, "hard-first-v1"), (True, "hard_set_reduced"))
        self.assertFalse(prefer(new, deepcopy(new), "hard-first-v1")[0])

    def test_lost_validation_coverage_and_invalid_contract_rejected(self):
        old = {"quality": {"checks": []}, "constraints": {
            "hard_ids": [], "evaluated_ids": ["closed:A"], "contract_valid": True}}
        new = deepcopy(old)
        new["constraints"]["evaluated_ids"] = []
        self.assertEqual(prefer(old, new, "hard-first-v1")[1], "lost_validation_coverage")
        new["constraints"]["contract_valid"] = False
        self.assertEqual(prefer(old, new, "hard-first-v1")[1], "invalid_contract")

    def test_expired_and_absent_facts_unknown_explicit_exclusion_checked(self):
        s = sample_snapshot()
        s["official_map"] = {"甲园": {"close_day": "周一", "valid_until": "2020-01-01"}}
        s["constraints"]["excluded_spots"] = ["甲园"]
        result = assess_constraints(sample_plan(), s)
        self.assertIn("closed:甲园", result["unknown_ids"])
        self.assertEqual(result["hard_ids"], ["excluded:甲园:1"])

    def test_zero_budget_zero_factory_or_requests(self):
        rid = self.create(max_attempts=0)
        factory = Mock(side_effect=AssertionError("factory must not be used"))
        with self.assertRaises(BudgetExhausted), self.store.lease(rid):
            execute(self.store, rid, client_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.store.summary(rid)["http_attempts"], 0)
        self.assertEqual(self.store.get(rid)["status"], "budget_exhausted")

    def test_sdk_retry_disabled_and_invalid_json_usage_counted(self):
        rid = self.create()
        m = self.model(["invalid JSON", sample_plan()])
        self.run_graph(rid, m)
        summary = self.store.summary(rid)
        self.assertEqual(summary["http_attempts"], len(m.requests))
        self.assertEqual(summary["application_retries"], 1)
        self.assertEqual(summary["sdk_retries"], 0)
        self.assertEqual(summary["known_prompt_tokens"], 10)

    def test_unknown_usage_kept_null(self):
        rid = self.create()
        m = self.model([sample_plan()], usage=False)
        self.run_graph(rid, m)
        attempt = next(e for e in self.store.events(rid) if e["kind"] == "http_attempt")
        self.assertIsNone(attempt["prompt_tokens"])
        self.assertEqual(self.store.summary(rid)["unknown_usage_attempts"], 1)

    def test_timeout_budget_does_not_stack_sdk_retries(self):
        rid = self.create(max_attempts=2)
        m = self.model([httpx.ReadTimeout("synthetic"), httpx.ReadTimeout("synthetic")])
        with self.assertRaises(BudgetExhausted):
            self.run_graph(rid, m)
        self.assertEqual(len(m.requests), 2)
        self.assertEqual(self.store.summary(rid)["unknown_result_attempts"], 2)
        self.assertFalse(self.store.public(rid)["resumable"])

    def test_fatal_auth_does_not_retry(self):
        rid = self.create()
        m = self.model([401])
        with self.assertRaises(FatalModelError):
            self.run_graph(rid, m)
        self.assertEqual(len(m.requests), 1)
        self.assertEqual(self.store.get(rid)["status"], "fatal")

    def test_retry_count_survives_run_resume(self):
        rid = self.create()
        m = self.model(["bad"] * 3)
        with self.assertRaises(RunError):
            self.run_graph(rid, m)
        self.assertFalse(self.store.public(rid)["resumable"])
        m2 = self.model([sample_plan()])
        with self.assertRaises(RunError):
            self.run_graph(rid, m2)
        self.assertEqual(len(m2.requests), 0)

    def test_cancel_is_persistent_and_isolated(self):
        a, b = self.create(), self.create()
        self.store.cancel(a)
        with self.assertRaises(RunCancelled):
            self.run_graph(a, self.model([]))
        self.run_graph(b, self.model([sample_plan()]))
        self.assertEqual(self.store.summary(a)["http_attempts"], 0)
        self.assertEqual(self.store.summary(b)["http_attempts"], 1)
        self.assertFalse(self.store.public(a)["resumable"])

    def test_cancel_after_request_stops_before_next_node(self):
        rid = self.create()
        m = self.model([sample_plan()], callback=lambda *a: self.store.cancel(rid))
        with self.assertRaises(RunCancelled):
            self.run_graph(rid, m)
        self.assertEqual(len(m.requests), 1)
        self.assertIsNone(self.store.step(rid, "graph_output"))

    def test_same_run_lease_blocks_second_thread(self):
        rid = self.create()
        errors = []
        with self.store.lease(rid):
            self.assertTrue(self.store.busy(rid))
            def other():
                try:
                    with self.store.lease(rid):
                        errors.append("unexpected execution")
                except RunBusy:
                    errors.append("busy")
            thread = threading.Thread(target=other)
            thread.start(); thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["busy"])

    def test_atomic_budget_reservation(self):
        rid = self.create(max_attempts=3)
        results = []
        def reserve(i):
            try:
                self.store.reserve(rid, "test", str(i), "hash", "fixture")
                results.append("reserved")
            except BudgetExhausted:
                results.append("exhausted")
        threads = [threading.Thread(target=reserve, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertEqual(results.count("reserved"), 3)
        self.assertEqual(self.store.summary(rid)["http_attempts"], 3)

    def test_simultaneous_store_initialization_serializes_migration(self):
        target = self.root / "new-schema.db"
        errors = []
        barrier = threading.Barrier(4)
        def initialize():
            try:
                barrier.wait(5)
                RunStore(target)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=initialize) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        with RunStore(target).connect() as db:
            self.assertEqual(sum(row[1] == "implementation_hash" for row in db.execute("PRAGMA table_info(runs)")), 1)

    def test_lease_released_even_when_owner_cleanup_fails(self):
        rid = self.create()
        original = self.store.update
        def fail_cleanup(rid, **fields):
            if fields == {"owner": None}:
                raise RuntimeError("synthetic cleanup failure")
            return original(rid, **fields)
        with patch.object(self.store, "update", side_effect=fail_cleanup), self.assertRaises(RuntimeError):
            with self.store.lease(rid):
                pass
        self.assertFalse(self.store.busy(rid))
        with self.store.lease(rid):
            pass

    def test_parallel_runs_have_independent_checkpoints_and_usage(self):
        a, b = self.create(), self.create()
        barrier = threading.Barrier(2)
        ma = self.model([sample_plan()], callback=lambda *a: barrier.wait(5))
        mb = self.model([sample_plan()], usage=False, callback=lambda *a: barrier.wait(5))
        failures = []
        def run(rid, model):
            try: self.run_graph(rid, model)
            except Exception as exc: failures.append(exc)
        threads = [threading.Thread(target=run, args=pair) for pair in ((a, ma), (b, mb))]
        for t in threads: t.start()
        for t in threads: t.join(15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(failures, [])
        self.assertEqual(self.store.summary(a)["known_prompt_tokens"], 5)
        self.assertEqual(self.store.summary(a)["unknown_usage_attempts"], 0)
        self.assertEqual(self.store.summary(b)["unknown_usage_attempts"], 1)
        self.assertEqual(self.store.summary(b)["http_attempts"], 1)

    def child(self, rid, mode, responses, expected_code=0):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("tests_planning_worker.py")),
                                 str(self.store.path), rid, mode, json.dumps(responses, ensure_ascii=False)],
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=40)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        return result

    def test_subprocess_generate_checkpoint_resumes_without_call(self):
        rid = self.create(max_attempts=1)
        self.child(rid, "pause_generate", [sample_plan()])
        self.assertEqual(self.store.summary(rid)["http_attempts"], 1)
        self.assertTrue(self.store.public(rid)["resumable"])
        self.child(rid, "resume", [])
        self.assertTrue(self.store.step(rid, "graph_output")["complete"])
        self.assertEqual(self.store.summary(rid)["http_attempts"], 1)

    def test_subprocess_quality_checkpoint_resumes_without_call(self):
        rid = self.create()
        self.child(rid, "pause_quality", [sample_plan()])
        self.child(rid, "resume", [])
        self.assertEqual(self.store.summary(rid)["http_attempts"], 1)

    def test_response_saved_before_graph_checkpoint_is_reused(self):
        rid = self.create(max_attempts=1)
        self.child(rid, "crash_after_response", [sample_plan()], expected_code=77)
        self.assertTrue(self.store.public(rid)["resumable"])
        self.child(rid, "resume", [])
        self.assertEqual(self.store.summary(rid)["http_attempts"], 1)
        self.assertTrue(any(e["kind"] == "response_reused" for e in self.store.events(rid)))

    def test_uncertain_request_window_marked_unknown_then_counted_again(self):
        rid = self.create()
        self.child(rid, "crash_before_response", [sample_plan()], expected_code=78)
        self.child(rid, "resume", [sample_plan()])
        self.assertEqual(self.store.summary(rid)["http_attempts"], 2)
        self.assertEqual(self.store.summary(rid)["unknown_result_attempts"], 1)

    def test_budget_not_refilled_after_restart(self):
        rid = self.create(max_attempts=1)
        self.child(rid, "pause_generate", [sample_plan(("甲园",))])
        self.child(rid, "resume", [sample_plan()], expected_code=2)
        self.assertEqual(self.store.summary(rid)["http_attempts"], 1)

    def test_tampered_expired_or_incompatible_snapshot_rejected(self):
        rid = self.create()
        with self.store.connect() as db:
            db.execute("UPDATE runs SET input_hash='wrong' WHERE run_id=?", (rid,))
        with self.assertRaises(RunError): self.store.validate(rid)
        rid2 = self.create(ttl_seconds=0.01)
        with self.store.connect() as db:
            db.execute("UPDATE runs SET expires_at=0 WHERE run_id=?", (rid2,))
        with self.assertRaises(RunError): self.store.validate(rid2)
        rid3 = self.create()
        with self.store.connect() as db:
            db.execute("UPDATE runs SET graph_version='incompatible' WHERE run_id=?", (rid3,))
        with self.assertRaises(RunError): self.store.validate(rid3)
        rid4 = self.create()
        with self.store.connect() as db:
            db.execute("UPDATE runs SET implementation_hash='other implementation' WHERE run_id=?", (rid4,))
        with self.assertRaises(RunError): self.store.validate(rid4)

    def finalizer(self, rid, output):
        from core import knowledge
        from service import planning_flow
        reports = self.root / "reports"
        reports.mkdir(exist_ok=True)
        return (patch.object(knowledge, "_DB_PATH", self.root / "knowledge.db"),
                patch.object(planning_flow, "REPORT_DIR", reports),
                patch.object(planning_flow.rag, "available", return_value=False))

    def test_finalizer_registration_failure_then_idempotent_retry(self):
        from core import knowledge
        from service import planning_flow
        rid = self.create()
        output = self.run_graph(rid, self.model([sample_plan()]))
        a, b, c = self.finalizer(rid, output)
        with a, b, c, self.store.lease(rid):
            original = knowledge.register_report
            def fail_after_write(*a, **k):
                original(*a, **k)
                raise RuntimeError("synthetic crash after database side effect")
            with patch.object(knowledge, "register_report", side_effect=fail_after_write):
                with self.assertRaises(RuntimeError): planning_flow.finalize_run(self.store, rid, output)
            result = planning_flow.finalize_run(self.store, rid, output)
            again = planning_flow.finalize_run(self.store, rid, output)
            self.assertEqual(result["report_name"], again["report_name"])
            self.assertTrue(Path(result["report_path"]).exists())
            with knowledge._conn() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM report_registry").fetchone()[0], 1)
            self.assertEqual(result["postprocessing"]["index_written"], "skipped:no_embedding")
            self.assertEqual(self.store.summary(rid)["http_attempts"], 1)
            self.assertTrue(result["quality"]["constraint_coverage"]["unknown_ids"])
            self.assertTrue(any("未判定" in x for x in result["quality"]["unresolved"]))

    def test_index_repair_checks_all_keys_not_any_prefix(self):
        from core import knowledge, rag, embeddings, vector_store
        rid = self.create()
        output = self.run_graph(rid, self.model([sample_plan()]))
        from service import planning_flow
        result = planning_flow.build_result(self.store, rid, output)
        chunks = rag.chunks_from_trip_plan(result["trip_plan"], rag._result_key(result))
        self.assertGreater(len(chunks), 1)
        with patch.object(knowledge, "_DB_PATH", self.root / "vectors.db"):
            embeddings.set_embedder(lambda texts: [[1.0, 0.0] for _ in texts])
            try:
                first = chunks[0]
                vector_store.upsert_items(rag.CORPUS_COLLECTION, [dict(first, vec=[1.0, 0.0])])
                result1 = rag.index_result_complete(result)
                self.assertTrue(result1["complete"])
                self.assertEqual(vector_store.count(rag.CORPUS_COLLECTION), len(chunks))
                self.assertEqual(rag.index_result_complete(result)["written"], 0)
            finally: embeddings.reset_embedder()

    def test_skipped_embedding_can_be_completed_after_configuration(self):
        from service import planning_flow
        rid = self.create(max_attempts=1)
        output = self.run_graph(rid, self.model([sample_plan()]))
        a, b, c = self.finalizer(rid, output)
        with a, b, c, self.store.lease(rid):
            first = planning_flow.finalize_run(self.store, rid, output)
        self.assertEqual(first["postprocessing"]["index_written"], "skipped:no_embedding")
        with patch.object(planning_flow.rag, "available", return_value=False):
            self.assertFalse(self.store.public(rid)["resumable"])
        a, b, _ = self.finalizer(rid, output)
        with a, b, patch.object(planning_flow.rag, "available", return_value=True), \
                patch.object(planning_flow.rag, "index_result_complete", return_value={"complete": True}) as index:
            self.assertTrue(self.store.public(rid)["resumable"])
            with self.store.lease(rid):
                result = planning_flow.finalize_run(self.store, rid, output)
            self.assertEqual(result["postprocessing"]["index_written"], "done")
            self.assertFalse(self.store.public(rid)["resumable"])
            index.assert_called_once()
        self.assertEqual(self.store.summary(rid)["http_attempts"], 1)

    def test_resume_api_reuses_snapshot_and_no_crawl_or_session_reset(self):
        from fastapi.testclient import TestClient
        from api_server import app
        from core import knowledge
        from service import planning_flow, research
        from crawler import base
        rid = self.create(job_id="fixture-job", max_attempts=1)
        self.run_graph(rid, self.model([sample_plan()]), pause_after=["quality"])
        a, b, c = self.finalizer(rid, {})
        with a, b, c, patch.object(planning_flow, "default_store", return_value=self.store), \
             patch("core.planning_runs.default_store", return_value=self.store), \
             patch.object(base, "reset_session", side_effect=AssertionError("must not reset")), \
             patch("service.trip._try_crawl", side_effect=AssertionError("must not crawl")):
            client = TestClient(app)
            self.assertTrue(client.get(f"/api/planning-runs/{rid}").json()["resumable"])
            accepted = client.post(f"/api/planning-runs/{rid}/resume")
            self.assertEqual(accepted.status_code, 202, accepted.text)
            # 等线程完成；只等待测试同步事件，不使用线上模型/浏览器。
            for _ in range(200):
                if self.store.get(rid)["status"] == "done": break
                threading.Event().wait(0.01)
            self.assertEqual(self.store.get(rid)["status"], "done")
            job = client.get("/api/jobs/fixture-job").json()
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["planning_run_id"], rid)
            self.assertEqual(self.store.summary(rid)["http_attempts"], 1)
            # 未配置embedding如实跳过；配置后再开放同run补做。
            self.assertEqual(job["result"]["postprocessing"]["index_written"], "skipped:no_embedding")
        research.JOBS.pop("fixture-job", None)

    def test_original_trip_entry_auto_and_explicit_reach_graph_and_report(self):
        from contextlib import ExitStack
        from core import knowledge, llm
        from core.models import VideoItem
        from core.evidence import bind_point, registry_for
        from pipeline.planner import empty_profile, empty_digest
        from service import planning_flow, trip, research
        for explicit in (True, False):
            with self.subTest(explicit=explicit), ExitStack() as stack:
                jid = "fixture-business-" + str(explicit)
                names = ["甲园", "乙馆"]
                items = {n: [VideoItem(n, "https://fixture.test/" + str(i), description="园内有绿树可看")]
                         for i, n in enumerate(names)}
                def point(item):
                    reg = registry_for(item)
                    p = bind_point({"claim": "园内有绿树可看", "topic": "打卡", "stance": "推荐",
                                    "evidence_id": next(iter(reg)), "excerpt": "园内有绿树可看"}, reg, item.url)
                    p.update(support_status="supported", n_sources=1, conf_level="低置信度", confidence=0.3)
                    return p
                bundle = {"categories": {n: "景点" for n in names},
                          "verify_results": [{"name": n, "verdict": "keep", "evidence": "弱"} for n in names],
                          "kept": names.copy(), "dropped": [], "funnel": {"synthetic": True}, "unresolved": [],
                          "researched": {n: (items[n], [point(items[n][0])]) for n in names}}
                profile = empty_profile() | {"duration_hours": 1, "best_time_slot": "任意"}
                m = self.model([sample_plan()])
                reports = self.root / "business-reports"
                reports.mkdir(exist_ok=True)
                patches = [patch.object(knowledge, "_DB_PATH", self.root / "business.db"),
                           patch.object(planning_flow, "REPORT_DIR", reports),
                           patch.object(planning_flow, "default_store", return_value=self.store),
                           patch.object(trip, "PLANNING_GRAPH_ENABLED", True),
                           patch.object(trip, "PLANNING_POLICY", "hard-first-v1"),
                           patch.object(trip, "_city_guide_layer", return_value={}),
                           patch.object(trip.rag, "index_city_guide", return_value=0),
                           patch.object(trip.rag, "available", return_value=False),
                           patch.object(trip, "generate_candidates", return_value=bundle["verify_results"]),
                           patch.object(trip, "_run_candidate_funnel", return_value=bundle),
                           patch.object(trip, "candidate_foods", return_value=[]),
                           patch.object(trip, "_try_crawl", side_effect=lambda name, *a, **k: items[name]),
                           patch.object(trip, "extract_points", side_effect=lambda item: [point(item)]),
                           patch.object(trip, "annotate_confidence", side_effect=lambda pts, **k: pts),
                           patch.object(trip, "build_spot_profile", side_effect=lambda *a, **k: deepcopy(profile)),
                           patch.object(trip, "build_review_digest", side_effect=lambda *a, **k: empty_digest()),
                           patch.object(trip, "transport_hints", return_value=[]),
                           patch.object(trip, "load_city_facts", return_value={}),
                           patch.object(trip, "douyin_enabled", return_value=False),
                           patch.object(trip.geo, "available", return_value=False),
                           patch.object(trip.entity_match, "resolve_prior_records", return_value={}),
                           patch.object(trip.entity_match, "register_entity", return_value=None),
                           patch.object(trip, "save_raw", return_value=str(self.root / "synthetic.json")),
                           patch.object(llm, "planning_client_and_model", side_effect=m.factory)]
                for item in patches: stack.enter_context(item)
                research.JOBS[jid] = {"id": jid, "mode": "trip", "kind": "trip", "status": "running",
                                      "created_at": "2026-10-14T12:00:00", "log": [], "cancel_requested": False}
                trip._run_trip(jid, "合成测试城", 1, "", names if explicit else None, "")
                job = research.JOBS[jid]
                self.assertEqual(job["status"], "done", str(job.get("error")) + str(job["log"]))
                self.assertTrue(Path(job["result"]["report_path"]).exists())
                self.assertEqual(job["result"]["planning_usage"]["http_attempts"], 1)
                self.assertTrue(job.get("planning_run_id"))
                self.assertEqual(job["result"]["trip_plan"]["snap"]["user_spots"], names if explicit else None)
                research.JOBS.pop(jid, None)


if __name__ == "__main__":
    unittest.main()
