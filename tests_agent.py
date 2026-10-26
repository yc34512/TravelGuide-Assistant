"""S4 工具 Agent 离线验收：真实 function-call 回填、权限、预算与证据核查。

模型 HTTP 由 OpenAI SDK + MockTransport 模拟；不调用真实付费模型。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx
from openai import OpenAI

from core.storage.reports import SQLiteReportStore
from core.storage.tasks import SQLiteTaskStore
from pipeline.ask_agent import run_agent, run_agent_job
from pipeline.ask_tools import ToolBudget, ToolContext, execute


def report_result(name="行程_甲_1.md", spot="甲园"):
    return {
        "report_name": name, "markdown": "# 报告",
        "quality": {"passed": True, "unresolved": []},
        "trip_plan": {
            "meta": {"city": "甲城", "days": 1, "facts_cutoff": "2026-10-01",
                     "generated_at": "2026-10-01T10:00:00"},
            "itinerary": [{"day": 1, "blocks": [
                {"slot": "上午", "spot": spot},
                {"slot": "下午", "spot": "乙馆"},
            ]}],
            "catalog": {"poi": {spot: {"ticket_price": 10,
                                       "official": {"source_url": "https://official.test/1"}},
                                "乙馆": {"ticket_price": 20,
                                        "official": {"source_url": "https://official.test/2"}}},
                        "food": {}},
            "quality": {"passed": True, "score": 100, "unresolved": [],
                        "constraint_coverage": {"hard_ids": []}},
            "snap": {
                "pitfall": [{"claim": "需预约", "quote": "必须提前预约",
                             "source": "https://ugc.test/1", "support_status": "supported",
                             "evidence_id": "ev-1", "confidence": "多源一致"}],
                "digests": {spot: {"quote_ids": [], "quote_points": []}},
            },
        },
    }


def tool_response(name, args, call_id="call-1"):
    return {
        "id": "chatcmpl-tool", "object": "chat.completion", "created": 0,
        "model": "fixture-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [{
                                     "id": call_id, "type": "function",
                                     "function": {"name": name,
                                                  "arguments": json.dumps(args,
                                                                          ensure_ascii=False)}}]}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


def final_response(content):
    return {
        "id": "chatcmpl-final", "object": "chat.completion", "created": 0,
        "model": "fixture-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
    }


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.client = OpenAI(
            api_key="synthetic-not-a-secret", base_url="https://fixture.test/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        if not self.responses:
            raise AssertionError("unexpected extra model request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    def factory(self):
        return self.client, "fixture-model"

    def close(self):
        self.client.close()


class AgentToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = SQLiteReportStore(Path(self.tmp.name) / "reports.db")
        self.tasks = SQLiteTaskStore(Path(self.tmp.name) / "tasks.db")
        self.version = self.reports.register_result(
            job_id="report-job", owner_id="alice", result=report_result())
        self.ctx = ToolContext(owner_id="alice", report=self.version, store=self.tasks,
                               job_id="agent-job", budget=ToolBudget())

    def test_read_report_and_poi_ids(self):
        out = execute("read_report", {}, self.ctx)
        self.assertTrue(out["ok"])
        self.assertEqual(out["source_version"], self.version["version_id"])
        ids = {p["id"] for p in out["data"]["pois"]}
        self.assertEqual(len(ids), 2)
        self.assertIn("ev-1", out["data"]["evidence_ids"])

    def test_get_evidence_returns_exact_quote_and_rejects_unknown(self):
        out = execute("get_evidence", {"evidence_ids": ["ev-1", "fake-id"]}, self.ctx)
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["evidence"][0]["quote"], "必须提前预约")
        self.assertEqual(out["data"]["missing"], ["fake-id"])
        self.assertEqual(out["evidence_refs"], ["ev-1"])

    def test_query_route_only_accepts_report_poi_ids_and_respects_budget(self):
        pois = list({p["id"] for p in execute("read_report", {}, self.ctx)["data"]["pois"]})
        seen = []

        def route_fn(origin, dest, mode, departure):
            seen.append((origin, dest, mode, departure))
            return {"advice": "公交约 30 分钟", "cache_hit": False,
                    "as_of": "2026-10-01T10:00:00"}

        self.ctx.route_fn = route_fn
        self.ctx.budget = ToolBudget(max_tool_calls=5, max_route_calls=1)
        out = execute("query_route", {"origin_poi_id": pois[0], "destination_poi_id": pois[1],
                                      "mode": "transit"}, self.ctx)
        self.assertTrue(out["ok"])
        self.assertEqual(seen[0][2], "transit")
        again = execute("query_route", {"origin_poi_id": pois[0],
                                        "destination_poi_id": pois[1],
                                        "mode": "transit"}, self.ctx)
        self.assertEqual(again["error_code"], "route_budget_exhausted")
        bad = execute("query_route", {"origin_poi_id": "https://evil.test",
                                      "destination_poi_id": pois[1],
                                      "mode": "transit"}, self.ctx)
        self.assertEqual(bad["error_code"], "invalid_poi_id")

    def test_unknown_tool_and_wrong_owner_report(self):
        self.assertEqual(execute("rm_rf", {}, self.ctx)["error_code"], "unknown_tool")
        out = execute("read_report", {"report_id": "other", "version_id": "other"}, self.ctx)
        self.assertEqual(out["error_code"], "not_found")


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = SQLiteReportStore(Path(self.tmp.name) / "reports.db")
        self.tasks = SQLiteTaskStore(Path(self.tmp.name) / "tasks.db")
        self.version = self.reports.register_result(
            job_id="report-job", owner_id="alice", result=report_result())
        agent = self.tasks.submit(owner_id="alice", idempotency_key="agent",
                                  kind="agent_ask", queue_name="query",
                                  payload={"question": "q", "job_id": "report-job"})
        self.agent_job = self.tasks.get(agent["id"], "alice")
        self.ctx = ToolContext(owner_id="alice", report=self.version, store=self.tasks,
                               job_id=self.agent_job["id"], budget=ToolBudget())

    def model(self, responses):
        m = ScriptedModel(responses)
        self.addCleanup(m.close)
        return m

    def test_real_tool_calls_are_executed_and_fed_back(self):
        final = json.dumps({"answer": "报告写明必须提前预约。", "evidence_ids": ["ev-1"],
                            "limitations": []}, ensure_ascii=False)
        m = self.model([
            tool_response("read_report", {}),
            tool_response("get_evidence", {"evidence_ids": ["ev-1"]}, "call-2"),
            final_response(final),
        ])
        result = run_agent("甲园需要预约吗？", self.ctx, client_factory=m.factory)
        self.assertEqual(result["evidence_refs"], ["ev-1"])
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertEqual(result["tool_calls"][1]["tool"], "get_evidence")
        self.assertEqual(result["usage"]["chat_attempts"], 3)
        self.assertIn("必须提前预约", result["answer"])
        events = [e for e in self.tasks.events(self.agent_job["id"])
                  if e["event_type"] == "agent_tool"]
        self.assertEqual(len(events), 2)
        # 第二次请求必须真的带上第一次工具结果（不是模型自己编的）
        contents = [m_["content"] for m_ in m.requests[1]["messages"] if m_["role"] == "tool"]
        self.assertTrue(any("ev-1" in c for c in contents))
        self.assertIn("tools", m.requests[0])

    def test_unknown_evidence_ids_are_rejected(self):
        final = json.dumps({"answer": "编造的引文", "evidence_ids": ["fake-id"],
                            "limitations": []}, ensure_ascii=False)
        m = self.model([final_response(final)])
        result = run_agent("问题", self.ctx, client_factory=m.factory)
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(result["unknown_evidence_ids"], ["fake-id"])
        self.assertTrue(any("不存在的证据编号" in x for x in result["limitations"]))

    def test_unknown_tool_error_is_visible_and_loop_continues(self):
        final = json.dumps({"answer": "无法执行该工具。", "evidence_ids": [],
                            "limitations": ["工具不可用"]}, ensure_ascii=False)
        m = self.model([tool_response("delete_everything", {}), final_response(final)])
        result = run_agent("问题", self.ctx, client_factory=m.factory)
        self.assertEqual(result["tool_calls"][0]["error_code"], "unknown_tool")
        self.assertEqual(result["stop_reason"], "final_answer")

    def test_budget_exhaustion_stops_without_extra_model_request(self):
        self.ctx.budget = ToolBudget(max_tool_calls=1, max_route_calls=0,
                                     max_embed_attempts=0)
        m = self.model([tool_response("read_report", {})])
        result = run_agent("问题", self.ctx, client_factory=m.factory)
        self.assertEqual(result["usage"]["chat_attempts"], 1)
        self.assertEqual(result["stop_reason"], "tool_budget_exhausted")
        self.assertTrue(any("停止原因" in x for x in result["limitations"]))
        self.assertEqual(len(m.requests), 1)

    def test_search_knowledge_uses_embeddings_and_scope(self):
        from core import embeddings, knowledge, rag
        orig_db = knowledge._DB_PATH
        knowledge._DB_PATH = Path(self.tmp.name) / "kb.db"
        self.addCleanup(lambda: setattr(knowledge, "_DB_PATH", orig_db))
        embeddings.set_embedder(lambda texts: [[float(len(t)), 1.0] for t in texts])
        self.addCleanup(embeddings.reset_embedder)
        rag.index_result(self.version["result"])
        out = execute("search_knowledge", {"query": "预约", "scope": "report",
                                           "top_k": 3}, self.ctx)
        self.assertTrue(out["ok"])
        self.assertTrue(all(h["ns"].startswith("evidence-v1:") for h in out["data"]))
        city = execute("search_knowledge", {"query": "预约", "scope": "city"},
                       self.ctx)
        self.assertFalse(city["ok"])
        self.assertEqual(city["error_code"], "no_relevant_knowledge")

    def test_run_agent_job_resolves_owner_report(self):
        from unittest import mock
        import core.storage as storage
        payload = {"question": "需要预约吗？", "job_id": "report-job"}
        job = {"id": self.agent_job["id"], "owner_id": "alice", "payload": payload}
        final = json.dumps({"answer": "需要。", "evidence_ids": ["ev-1"],
                            "limitations": []}, ensure_ascii=False)
        m = self.model([tool_response("get_evidence", {"evidence_ids": ["ev-1"]}),
                        final_response(final)])
        with mock.patch.object(storage, "report_store", return_value=self.reports):
            result = run_agent_job(job, type("Ctx", (), {"store": self.tasks})(),
                                   client_factory=m.factory)
        self.assertEqual(result["evidence_refs"], ["ev-1"])
        self.assertEqual(result["report_id"], self.version["report_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)