"""本轮服务化升级的离线回归：采集状态隔离与 FAISS 索引对应（S0）。

不联网、不调 LLM、不写真实知识库。FAISS 未安装时用替身验证映射逻辑，
真实包集成项按可用性跳过并明确原因。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


class TestCrawlerSessionIsolation(unittest.TestCase):
    def tearDown(self):
        from crawler import base
        base.clear_account_risk()
        base.end_task()
        base.reset_session()
        base.end_task()

    def test_task_b_does_not_clear_running_task_a(self):
        from crawler import base

        base.clear_account_risk()
        base.end_task()
        self.assertTrue(base.reset_session("task-a"))
        base.bump_navigation()
        base.stop_session()
        count_a = base.nav_count()

        # 任务 B 初始化：不得清空 A 的计数，也不得解除账号会话熔断。
        self.assertFalse(base.reset_session("task-b"))
        self.assertEqual(base.nav_count(), count_a)
        self.assertTrue(base.session_stopped())
        self.assertEqual(base.active_task(), "task-a")

        # A 收口后，B 才拿到自己的预算；熔断仍持续（会话级）。
        base.end_task("task-a")
        self.assertTrue(base.reset_session("task-b"))
        self.assertEqual(base.nav_count(), 0)
        self.assertTrue(base.session_stopped())

    def test_new_task_keeps_captcha_breaker_until_explicit_clear(self):
        from crawler import base

        base.stop_session()
        base.reset_session()
        self.assertTrue(base.session_stopped())
        base.clear_account_risk()
        self.assertFalse(base.session_stopped())

    def test_same_task_begin_is_idempotent(self):
        from crawler import base

        base.end_task()
        base.reset_session("task-c")
        base.bump_navigation()
        self.assertTrue(base.reset_session("task-c"))   # 同一任务不重复清零
        self.assertEqual(base.nav_count(), 1)
        base.end_task("task-c")
        self.assertIsNone(base.active_task())


class _FakeFaiss:
    """只实现 vector_store 用到的 API；search 故意按相似度排序返回。"""

    def __init__(self):
        self.added = None

    def normalize_L2(self, arr):
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        np.divide(arr, norms, out=arr, where=norms > 0)

    def IndexFlatIP(self, dim):
        outer = self

        class _Index:
            def add(self, arr):
                outer.added = arr.copy()

            def search(self, q, k):
                scores = (outer.added @ q[0]).astype("float32")
                order = np.argsort(-scores)[:k]
                return scores[order][None, :], order[None, :]

        return _Index()


class TestFaissIndexMapping(unittest.TestCase):
    def setUp(self):
        from core import knowledge
        self._orig = knowledge._DB_PATH
        knowledge._DB_PATH = Path(tempfile.mkdtemp()) / "vec.db"

    def tearDown(self):
        from core import knowledge
        knowledge._DB_PATH = self._orig

    def _fake_modules(self):
        fake = _FakeFaiss()
        module = types.SimpleNamespace(
            normalize_L2=fake.normalize_L2, IndexFlatIP=fake.IndexFlatIP)
        return fake, module

    def test_scores_are_mapped_back_to_original_rows(self):
        from core import vector_store

        fake, module = self._fake_modules()
        with mock.patch.dict(sys.modules, {"faiss": module}):
            # 原始顺序与相似度排序不同：第二行最相似、第一行最不相似。
            scores = vector_store._cosine_scores_faiss(
                [1.0, 0.0], [[0.0, 1.0], [1.0, 0.0], [0.7, 0.7]])
        self.assertIsNotNone(scores)
        self.assertAlmostEqual(scores[0], 0.0, places=4)
        self.assertAlmostEqual(scores[1], 1.0, places=4)
        self.assertAlmostEqual(scores[2], 0.7071, places=4)

    def test_search_returns_correct_text_for_each_score(self):
        from core import vector_store

        _, module = self._fake_modules()
        col = "s0_faiss"
        vector_store.clear(col)
        vector_store.upsert_items(col, [
            {"key": "far", "text": "无关", "vec": [0.0, 1.0]},
            {"key": "near", "text": "最相关", "vec": [1.0, 0.0]},
            {"key": "mid", "text": "中等", "vec": [0.7, 0.7]},
        ])
        with mock.patch.dict(sys.modules, {"faiss": module}):
            hits = vector_store.search(col, [1.0, 0.0], top_k=3)
        self.assertEqual([h["key"] for h in hits], ["near", "mid", "far"])
        by_key = {h["key"]: h["score"] for h in hits}
        self.assertGreater(by_key["near"], by_key["mid"])
        self.assertGreater(by_key["mid"], by_key["far"])

    def test_zero_and_bad_dimension_are_skipped_on_both_paths(self):
        from core import vector_store

        _, module = self._fake_modules()
        col = "s0_faiss_bad"
        vector_store.clear(col)
        vector_store.upsert_items(col, [
            {"key": "ok", "text": "有效", "vec": [1.0, 0.0]},
            {"key": "zero", "text": "零向量", "vec": [0.0, 0.0]},
            {"key": "dim", "text": "维度异常", "vec": [1.0, 0.0, 0.0]},
        ])
        with mock.patch.dict(sys.modules, {"faiss": module}):
            hits = vector_store.search(col, [1.0, 0.0], top_k=5)
        self.assertEqual([h["key"] for h in hits], ["ok"])

    def test_real_faiss_integration_if_installed(self):
        if importlib.util.find_spec("faiss") is None:
            self.skipTest("真实 faiss 未安装：本次仅验证替身映射，不宣称真实包集成")
        from core import vector_store

        col = "s0_faiss_real"
        vector_store.clear(col)
        vector_store.upsert_items(col, [
            {"key": "far", "text": "无关", "vec": [0.0, 1.0]},
            {"key": "near", "text": "最相关", "vec": [1.0, 0.0]},
        ])
        hits = vector_store.search(col, [1.0, 0.0], top_k=2)
        self.assertEqual(hits[0]["key"], "near")
        self.assertGreater(hits[0]["score"], hits[1]["score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)