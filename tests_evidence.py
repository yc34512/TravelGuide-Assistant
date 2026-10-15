"""证据链离线回归：伪造引用、断章取义、语义复核失败与所有引用出口。

模型与embedding只使用固定响应；不联网，不采集，不使用真实API配额。
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from core.evidence import (
    VERSION, bind_point, digest_quotes, evidence_valid, quote_bank, registry_for,
    select_quotes, support_rule_reason, usable_points,
)
from core.models import Comment, VideoItem


def item(text="成人票60元，65岁以上老人免票。", url="https://fixture.test/v1", **kw):
    return VideoItem("test", url, comments=[Comment(text)], **kw)


def point(text="成人票60元，65岁以上老人免票。", claim="成人票60元", source="https://fixture.test/v1",
          kind="comment", excerpt=None, **fields):
    video = item(text, source) if kind == "comment" else VideoItem("t", source, **{kind: text})
    reg = registry_for(video)
    eid = next(iter(reg))
    raw = dict(topic="门票", claim=claim, stance="中性", time_sensitive=True,
               evidence_id=eid, excerpt=excerpt or text)
    raw.update(fields)
    bound = bind_point(raw, reg, source)
    assert bound is not None
    return bound


def supported(*args, **kw):
    p = point(*args, **kw)
    p["support_status"] = "supported"
    return p


class TestLiteralEvidence(unittest.TestCase):
    def test_excerpt_is_copied_from_complete_source(self):
        p = point(excerpt="65岁以上老人免票")
        self.assertTrue(evidence_valid(p))
        self.assertEqual(p["quote"], "65岁以上老人免票")
        self.assertEqual(p["evidence"]["text"], "成人票60元，65岁以上老人免票。")
        self.assertEqual(p["source"], "https://fixture.test/v1")

    def test_same_prefix_and_fake_suffix_rejected(self):
        v = item("我们周六去景区游玩现场成人门票60元")
        reg = registry_for(v)
        raw = dict(claim="所有人免费", evidence_id=next(iter(reg)),
                   excerpt="我们周六去景区游玩现场成人门票全部免费")
        self.assertIsNone(bind_point(raw, reg, v.url))

    def test_no_truncation_before_validation(self):
        text = "这是原始评论内容" * 15
        v = item(text)
        raw = dict(claim="内容", evidence_id=next(iter(registry_for(v))), excerpt=text + "伪造尾部")
        self.assertIsNone(bind_point(raw, registry_for(v), v.url))

    def test_long_real_quote_keeps_context(self):
        text = "这是一条很长的原始评论" * 12 + "但不能因此说全年免费"
        p = point(text=text, claim="不能说全年免费")
        self.assertGreater(len(p["quote"]), 80)
        self.assertEqual(p["quote"], text)

    def test_empty_excerpt_rejected(self):
        v = item()
        self.assertIsNone(bind_point(dict(evidence_id=next(iter(registry_for(v))), excerpt=""), registry_for(v), v.url))

    def test_unknown_id_rejected(self):
        v = item()
        self.assertIsNone(bind_point(dict(evidence_id="made_up", excerpt="成人票60元"), registry_for(v), v.url))

    def test_other_video_id_rejected(self):
        v1, v2 = item(), item(url="https://fixture.test/v2")
        raw = dict(evidence_id=next(iter(registry_for(v2))), excerpt="成人票60元")
        self.assertIsNone(bind_point(raw, registry_for(v1), v1.url))

    def test_forged_source_rejected(self):
        v = item()
        raw = dict(evidence_id=next(iter(registry_for(v))), excerpt="成人票60元", source="https://fake.test")
        self.assertIsNone(bind_point(raw, registry_for(v), v.url))

    def test_only_first_fifty_comments_are_evidence(self):
        v = item()
        v.comments = [Comment(f"第{i}条评论") for i in range(51)]
        reg = registry_for(v)
        self.assertEqual(len(reg), 50)
        self.assertIsNone(bind_point(dict(quote="第50条评论", claim="最后一条"), reg, v.url))

    def test_empty_quote_without_video_evidence_does_not_bypass(self):
        v = item()
        self.assertIsNone(bind_point(dict(quote="", claim="景区周一闭馆"), registry_for(v), v.url))

    def test_description_can_support_without_comment_quote(self):
        p = point("成人票60元", kind="description")
        self.assertTrue(evidence_valid(p))
        self.assertEqual(p["quote"], "")
        self.assertEqual(p["evidence"]["kind"], "description")

    def test_transcript_can_support_without_comment_quote(self):
        p = point("景区周一开放", claim="周一开放", kind="transcript")
        self.assertTrue(evidence_valid(p))
        self.assertFalse(p["quote"])

    def test_changed_snapshot_fails_integrity_check(self):
        p = point()
        p["evidence"]["text"] += "伪造内容"
        self.assertFalse(evidence_valid(p))

    def test_changed_quote_fails_integrity_check(self):
        p = point()
        p["quote"] += "假结论"
        self.assertFalse(evidence_valid(p))

    def test_changed_offset_fails(self):
        p = point()
        p["evidence"]["start"] = True
        self.assertFalse(evidence_valid(p))

    def test_old_marked_matched_without_snapshot_is_not_verified(self):
        self.assertFalse(evidence_valid(dict(grounding_status="matched", quote="原话", source="u")))

    def test_same_video_updated_comment_gets_distinct_evidence_id(self):
        self.assertNotEqual(point("成人票60元")["evidence_id"], point("成人票80元")["evidence_id"])

    def test_number_rule_blocks_changed_amount(self):
        self.assertIn("数字", support_rule_reason(point(claim="成人票80元")))

    def test_age_rule_blocks_dropped_condition(self):
        self.assertIn("年龄", support_rule_reason(point(claim="老人免票")))

    def test_valid_age_and_amount_pass_basic_rules(self):
        self.assertEqual(support_rule_reason(point(claim="65岁以上老人免票")), "")

    def test_long_context_is_pending_without_truncating(self):
        self.assertIn("过长", support_rule_reason(point("长" * 6001, claim="长")))


class TestSupportReview(unittest.TestCase):
    def review(self, pts, response):
        from pipeline.verify import annotate_confidence
        with patch("pipeline.verify.chat_json", return_value=response) as call:
            out = annotate_confidence(pts)
        return out, call

    def test_supported_points_count_only_real_sources(self):
        pts = [point(source=f"https://fixture.test/v{i}") for i in range(3)]
        response = dict(groups=[[1, 2, 3]], conflicts=[], support=[dict(id=i, status="supported") for i in range(1, 4)])
        out, call = self.review(pts, response)
        self.assertEqual(len(usable_points(out)), 3)
        self.assertTrue(all(p["n_sources"] == 3 for p in out))
        self.assertEqual(call.call_count, 1)

    def test_supported_quote_and_contradicted_claim_are_distinct(self):
        p = point("有人说周一闭馆，但我们周一去正常开放。", claim="周一闭馆", excerpt="周一闭馆")
        out, call = self.review([p], dict(groups=[[1]], conflicts=[], support=[dict(id=1, status="contradicted")]))
        self.assertTrue(evidence_valid(out[0]))
        self.assertFalse(usable_points(out))
        self.assertEqual(out[0]["support_status"], "rejected")
        self.assertIn("但我们周一去正常开放", call.call_args.args[1])

    def test_rejected_source_cannot_inflate_confidence_or_conflicts(self):
        pts = [point(source=f"https://fixture.test/v{i}") for i in range(3)]
        response = dict(groups=[[1, 2, 3]], conflicts=[[1, 3]], support=[
            dict(id=1, status="supported"), dict(id=2, status="supported"), dict(id=3, status="unrelated")])
        out, _ = self.review(pts, response)
        self.assertEqual(out[0]["n_sources"], 2)
        self.assertEqual(out[0]["confidence"], "多源一致")
        self.assertEqual(out[2]["n_sources"], 0)

    def test_missing_support_response_is_pending(self):
        out, _ = self.review([point()], dict(groups=[[1]], conflicts=[]))
        self.assertEqual(out[0]["support_status"], "pending")
        self.assertFalse(usable_points(out))

    def test_model_error_is_pending_not_trusted(self):
        from pipeline.verify import annotate_confidence
        with patch("pipeline.verify.chat_json", side_effect=RuntimeError("offline")):
            out = annotate_confidence([point()])
        self.assertEqual(out[0]["support_status"], "pending")
        self.assertEqual(out[0]["conf_score"], 0)

    def test_duplicate_support_ids_are_pending(self):
        out, _ = self.review([point()], dict(groups=[[1]], conflicts=[], support=[
            dict(id=1, status="supported"), dict(id=1, status="contradicted")]))
        self.assertEqual(out[0]["support_status"], "pending")

    def test_numeric_rule_cannot_be_overridden_by_llm(self):
        out, call = self.review([point(claim="成人票80元")], dict(support=[dict(id=1, status="supported")]))
        self.assertEqual(out[0]["support_status"], "rejected")
        call.assert_not_called()

    def test_old_points_are_marked_unverified_and_quotes_cleared(self):
        out, call = self.review([dict(topic="门票", claim="全免费", quote="随便写", source="u")], {})
        self.assertEqual(out[0]["support_status"], "unverified")
        self.assertFalse(out[0]["quote"])
        call.assert_not_called()

    def test_reviewed_pending_point_can_be_retried(self):
        pending, _ = self.review([point()], {})
        out, _ = self.review(pending, dict(groups=[[1]], conflicts=[], support=[dict(id=1, status="supported")]))
        self.assertEqual(len(usable_points(out)), 1)

    def test_batch_context_is_complete_and_bounded(self):
        from pipeline.verify import BATCH_MAX_CHARS, _review_batches, _review_input
        pts = [point("原文" * 2400, claim="原文", source=f"https://fixture.test/v{i}") for i in range(12)]
        batches = list(_review_batches(pts))
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(batch) for _, batch in batches), len(pts))
        for _, batch in batches:
            self.assertLessEqual(len(_review_input(batch)), BATCH_MAX_CHARS)
            self.assertIn(batch[0]["evidence"]["text"], _review_input(batch))

    def test_batch_support_ids_are_remapped_and_failed_batch_stays_pending(self):
        from pipeline.verify import annotate_confidence
        pts = [point("原文" * 2400, claim="原文", source=f"https://fixture.test/v{i}") for i in range(12)]
        calls = [0]
        def fake(system, user, **kw):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("第一批失败")
            import re
            ids = [int(i) for i in re.findall(r"^\[(\d+)\]", user, re.M)]
            return dict(groups=[ids], conflicts=[], support=[dict(id=i, status="supported") for i in ids])
        with patch("pipeline.verify.chat_json", side_effect=fake):
            out = annotate_confidence(pts)
        self.assertGreater(calls[0], 1)
        self.assertEqual(out[0]["support_status"], "pending")
        self.assertEqual(out[-1]["support_status"], "supported")
        self.assertEqual(out[-1]["source"], pts[-1]["source"])


class TestQuoteExits(unittest.TestCase):
    def test_unknown_and_duplicate_quote_ids_are_filtered(self):
        p = supported()
        bank = quote_bank([p])
        records = select_quotes(["unknown", p["quote_id"], p["quote_id"]], bank)
        self.assertEqual([r["text"] for r in records], [p["quote"]])

    def test_pending_point_cannot_enter_quote_bank(self):
        self.assertEqual(quote_bank([point()]), {})

    def test_quote_bank_cannot_be_mutated_to_fake_text(self):
        p = supported()
        bank = quote_bank([p])
        bank[p["quote_id"]]["text"] += "编造后缀"
        self.assertEqual(select_quotes([p["quote_id"]], bank), [])

    def test_verifier_can_revoke_previously_available_quote(self):
        p = supported()
        bank = quote_bank([p])
        p["support_status"] = "pending"
        self.assertEqual(select_quotes([p["quote_id"]], bank), [])

    def test_digest_ignores_model_written_quote_and_refills_id(self):
        from pipeline.planner import _normalize_digest
        p = supported()
        out = _normalize_digest(dict(quote_ids=[p["quote_id"]], quotes=["虚构评价"]), quote_bank([p]), [p])
        self.assertEqual(out["quotes"], [p["quote"]])
        self.assertEqual(digest_quotes(out), [p["quote"]])

    def test_legacy_digest_literal_checker_rejects_extended_quote(self):
        from pipeline.planner import _normalize_digest
        out = _normalize_digest(dict(quotes=["真实原话但免费", "真实原话"]), ["真实原话"])
        self.assertEqual(out["quotes"], ["真实原话"])

    def test_digest_summary_requires_refs_and_matching_numbers(self):
        from pipeline.planner import _normalize_digest
        p = supported()
        out = _normalize_digest(dict(verdict="成人票80元", verdict_refs=[1], positive="凭空好评"), quote_bank([p]), [p])
        self.assertFalse(out["verdict"])
        self.assertFalse(out["positive"])

    def test_plan_quotes_are_scoped_to_current_spot(self):
        from pipeline.planner import _normalize_plan
        p1 = supported(source="https://fixture.test/a", stance="避雷")
        p2 = supported(source="https://fixture.test/b", stance="避雷")
        data = dict(days=[dict(slots=[dict(spot="甲", pitfall_quote_ids=[p2["quote_id"], p1["quote_id"]], pitfall_quotes=["伪造"])] )])
        out = _normalize_plan(data, {"甲"}, 1, {"甲": quote_bank([p1]), "乙": quote_bank([p2])})
        self.assertEqual(out["days"][0]["slots"][0]["pitfall_quotes"], [p1["quote"]])

    def test_plan_without_evidence_drops_all_generated_quotes(self):
        from pipeline.planner import _normalize_plan
        out = _normalize_plan(dict(days=[dict(slots=[dict(spot="甲", pitfall_quotes=["假原话"])])]), {"甲"}, 1)
        self.assertFalse(out["days"][0]["slots"][0]["pitfall_quotes"])

    def test_profile_and_digest_do_not_use_pending_claims(self):
        from pipeline.planner import build_review_digest, build_spot_profile
        with patch("pipeline.planner.chat_json") as call:
            self.assertIsNone(build_spot_profile("甲", [point()])["duration_hours"])
            self.assertFalse(build_review_digest("甲", [point()])["quotes"])
        call.assert_not_called()

    def test_legacy_rag_does_not_index_unverified_comment_quotes(self):
        from core.rag import chunks_from_trip_plan
        tp = dict(snap=dict(digests=dict(甲=dict(quotes=["伪造原话"])), pitfall=[dict(claim="旧结论", quote="假的")]))
        chunks = chunks_from_trip_plan(tp)
        self.assertFalse(any("伪造原话" in c["text"] or "假的" in c["text"] for c in chunks))
        self.assertTrue(all(c["meta"]["legacy_unverified"] for c in chunks))

    def test_compressed_ask_does_not_restore_legacy_quotes(self):
        from pipeline.ask import build_context
        result = dict(trip_plan=dict(meta=dict(city="甲"), catalog=dict(poi=dict(甲={})),
                      snap=dict(digests=dict(甲=dict(verdict="旧总评", quotes=["伪造原话"])),
                                pitfall=[dict(claim="旧结论", quote="假的")])))
        ctx = build_context(result, "甲")
        self.assertNotIn("伪造原话", ctx)
        self.assertNotIn("假的", ctx)
        self.assertIn("未完成原文复核", ctx)

    def test_pending_claims_cannot_enter_fact_glance(self):
        from pipeline.render import render_report
        p = point()
        p.update(support_status="pending", confidence="单源")
        text = render_report("甲", "待确认说明", [], [p])
        self.assertNotIn("成人票60元", text)

    def test_report_without_supported_evidence_does_not_call_llm(self):
        from pipeline.report import synthesize_report
        p = point()
        with patch("pipeline.report.chat_text") as call:
            text = synthesize_report("甲", [p])
        call.assert_not_called()
        self.assertIn("待确认", text)

    def test_extract_rejects_claim_instead_of_only_clearing_quote(self):
        from pipeline.extract import extract_points
        v = item()
        raw = dict(points=[dict(claim="全部免费", quote="虚构原话", topic="门票")])
        with patch("pipeline.extract.chat_json", return_value=raw):
            self.assertEqual(extract_points(v), [])

    def test_extract_uses_numbered_evidence_and_binds_url(self):
        from pipeline.extract import extract_points
        v = item()
        eid = next(iter(registry_for(v)))
        raw = dict(points=[dict(claim="成人票60元", topic="门票", evidence_id=eid, excerpt="成人票60元")])
        with patch("pipeline.extract.chat_json", return_value=raw) as call:
            out = extract_points(v)
        self.assertTrue(evidence_valid(out[0]))
        self.assertIn(eid, call.call_args.args[1])
        self.assertEqual(out[0]["source"], v.url)


if __name__ == "__main__":
    unittest.main()
