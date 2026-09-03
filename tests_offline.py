"""离线自测套件：不调 LLM、不开浏览器、不消耗额度、不写真实知识库。

用法：python tests_offline.py

覆盖：
- 知识库关键词归一化
- 报告速览清单（可取/不可取/有争议）
- 立场冲突代码兜底
- 交叉验证在 LLM 不可用时的降级（全部单源）
- 评论低质过滤逻辑
- 视频时效判断（发布较旧）
- 任务档案落库/还原（临时库）
- 任务取消原语与 API 防目录穿越
"""
import tempfile
import unittest
from pathlib import Path


class TestKnowledge(unittest.TestCase):
    def test_normalize(self):
        from core.knowledge import normalize_keyword

        self.assertEqual(normalize_keyword("西湖攻略"), "西湖")
        self.assertEqual(normalize_keyword("西湖 攻略"), "西湖")
        self.assertEqual(normalize_keyword("武功山旅游攻略"), "武功山")
        self.assertEqual(normalize_keyword("攻略"), "攻略")  # 归一化后为空保留原串

    def test_job_persistence_roundtrip(self):
        """record_job -> load_job -> list_jobs 全链路（临时库，不污染真实数据）。"""
        from core import knowledge

        tmp = Path(tempfile.mkdtemp()) / "test_kb.db"
        orig = knowledge._DB_PATH
        knowledge._DB_PATH = tmp
        try:
            job = {
                "id": "test123",
                "keyword": "西湖",
                "mode": "fast",
                "status": "done",
                "stage": "完成",
                "result": {"report_name": "x.md", "cache_hit": True},
                "error": None,
                "created_at": "2026-09-01T10:00:00",
            }
            knowledge.record_job(job)
            # UPSERT：重复落库不应产生两条记录
            knowledge.record_job({**job, "status": "cancelled"})

            loaded = knowledge.load_job("test123")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["status"], "cancelled")
            self.assertEqual(loaded["result"]["report_name"], "x.md")
            self.assertTrue(loaded["cache_hit"])
            self.assertIsNone(knowledge.load_job("不存在"))

            jobs = knowledge.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["id"], "test123")
        finally:
            knowledge._DB_PATH = orig

    def test_report_registry_merge(self):
        """行程报告登记后能进历史列表（与攻略报告合并，文件不存在则过滤）。"""
        import tempfile as _tf

        from core import knowledge

        tmp_db = Path(_tf.mkdtemp()) / "test_reg.db"
        tmp_md = Path(_tf.mkdtemp()) / "行程_测试.md"
        tmp_md.write_text("x", encoding="utf-8")
        orig = knowledge._DB_PATH
        knowledge._DB_PATH = tmp_db
        try:
            knowledge.register_report("行程·测试2天", str(tmp_md), 3, 10)
            rows = knowledge.list_history()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["keyword"], "行程·测试2天")
            self.assertEqual(rows[0]["report_path"], tmp_md.name)
            # 文件被删除后自动从列表消失（用户手动清理报告的兼容）
            tmp_md.unlink()
            self.assertEqual(knowledge.list_history(), [])
        finally:
            knowledge._DB_PATH = orig


class TestRender(unittest.TestCase):
    def test_quick_glance(self):
        from pipeline.render import _quick_glance

        pts = [
            {"claim": "苏堤早上人少", "stance": "推荐", "confidence": "多源一致"},
            {"claim": "拉客拍照别理", "stance": "避雷", "confidence": "多源一致"},
            {"claim": "门票说法不一", "stance": "中性", "confidence": "存分歧"},
            {"claim": "客观事实", "stance": "中性", "confidence": "单源"},
        ]
        glance = _quick_glance(pts)
        self.assertIn("值得做", glance)
        self.assertIn("别踩坑", glance)
        self.assertIn("有争议", glance)
        self.assertIn("苏堤早上人少", glance)
        self.assertNotIn("客观事实", glance)  # 中性不进速览
        self.assertEqual(_quick_glance([]), "")


class TestSmartSearch(unittest.TestCase):
    def test_rank_candidates(self):
        """点赞降序在前、无点赞垫后、按 video_id 去重、限量。"""
        from crawler.douyin import rank_candidates

        cands = [
            {"video_id": "a", "url": "u_a", "like_count": 10},
            {"video_id": "b", "url": "u_b", "like_count": None},
            {"video_id": "c", "url": "u_c", "like_count": 500},
            {"video_id": "a", "url": "u_a", "like_count": 10},  # 重复（多查询合并）
            {"video_id": "d", "url": "u_d", "like_count": 0},   # 0 赞视同未计分？不：0 为假值垫后，行为一致
            {"video_id": "e", "url": "u_e", "like_count": 30},
        ]
        self.assertEqual(rank_candidates(cands, 3), ["u_c", "u_e", "u_a"])
        self.assertEqual(rank_candidates(cands, 100), ["u_c", "u_e", "u_a", "u_b", "u_d"])
        self.assertEqual(rank_candidates([], 5), [])


class TestGapFill(unittest.TestCase):
    def test_missing_topics(self):
        from service.research import missing_topics

        full = [{"topic": "门票"}, {"topic": "交通"}, {"topic": "美食"}]
        self.assertEqual(missing_topics(full), [])
        self.assertEqual(missing_topics([{"topic": "美食"}]), ["门票", "交通"])
        self.assertEqual(missing_topics([{"topic": "门票"}]), ["交通"])
        self.assertEqual(missing_topics([{"topic": "交通"}, {"topic": "打卡"}]), ["门票"])
        self.assertEqual(missing_topics([]), ["门票", "交通"])
        # topic 缺失的要点不参与覆盖判定，不报错
        self.assertEqual(missing_topics([{"claim": "x"}]), ["门票", "交通"])


class TestSanitizeAuthor(unittest.TestCase):
    def test_author_reply_detection(self):
        from core.sanitize import parse_comment_block

        normal = "昵称\n...\n门票免费的\n1天前·北京\n35\n分享\n回复"
        text, like, is_author, c_time = parse_comment_block(normal)
        self.assertEqual(text, "门票免费的")
        self.assertEqual(like, 35)
        self.assertFalse(is_author)
        self.assertIsNotNone(c_time)  # "1天前"换算成日期（情感趋势用）

        author = "某网友\n作者\n...\n谢谢大家支持\n2小时前·浙江\n3\n分享\n回复"
        text, like, is_author, c_time = parse_comment_block(author)
        self.assertEqual(text, "谢谢大家支持")
        self.assertTrue(is_author)

        # 宁漏不误标：正文里含"作者"二字但不是独立标记行，不认定作者回复
        tricky = "昵称\n...\n作者是好人\n1天前\n5\n分享\n回复"
        text, like, is_author, c_time = parse_comment_block(tricky)
        self.assertEqual(text, "作者是好人")
        self.assertFalse(is_author)

        # 空块
        self.assertEqual(parse_comment_block(""), ("", None, False, None))


class TestVerify(unittest.TestCase):
    def test_stance_conflicts(self):
        from pipeline.verify import _stance_conflicts

        points = [
            {"stance": "推荐", "claim": "坐船值得"},
            {"stance": "避雷", "claim": "坐船是坑"},
            {"stance": "中性", "claim": "船票 70"},
        ]
        pairs = _stance_conflicts(points, [[1, 2, 3]])
        self.assertEqual(pairs, [(1, 2)])
        self.assertEqual(_stance_conflicts([{"stance": "推荐"}], [[1]]), [])

    def test_fallback_when_llm_unavailable(self):
        """LLM 调用抛异常时降级为全部单源，不阻断主流程。"""
        import pipeline.verify as v

        def boom(*a, **k):
            raise RuntimeError("no llm")

        orig = v.chat_json
        v.chat_json = boom
        try:
            out = v.annotate_confidence(
                [
                    {"topic": "门票", "claim": "免费", "stance": "中性",
                     "time_sensitive": True, "source": "u1"},
                    {"topic": "门票", "claim": "免费", "stance": "中性",
                     "time_sensitive": True, "source": "u2"},
                ]
            )
        finally:
            v.chat_json = orig
        self.assertTrue(all(p["confidence"] == "单源" for p in out))


class TestCrawlerFilter(unittest.TestCase):
    def test_comment_filter_logic(self):
        """低质过滤 + 保底放宽（复现 _fetch_comments 尾部逻辑）。"""
        from core.models import Comment
        from crawler.douyin import MIN_COMMENTS_KEEP, MIN_COMMENT_LIKES

        ranked = sorted(
            [Comment("a", 10), Comment("b", 0), Comment("c", 3), Comment("d", None)],
            key=lambda c: c.like_count or 0,
            reverse=True,
        )
        keep = [c for c in ranked if (c.like_count or 0) >= MIN_COMMENT_LIKES]
        self.assertEqual(len(keep), 2)
        self.assertLess(len(keep), MIN_COMMENTS_KEEP)  # 不足保底 -> 放宽
        keep = ranked
        self.assertEqual(keep[0].text, "a")


class TestExtract(unittest.TestCase):
    def test_is_stale(self):
        from pipeline.extract import _is_stale

        self.assertTrue(_is_stale("2020-01-01"))
        self.assertFalse(_is_stale("2026-08-01"))
        self.assertFalse(_is_stale(None))
        self.assertFalse(_is_stale("无效日期"))

    def test_sanitize_stance(self):
        """历史叙事被误标"避雷"时降为中性；带警示词的真避坑与推荐不受影响。"""
        from pipeline.extract import _sanitize_stance

        # 纯历史叙事：降级（这是用户报的 case）
        self.assertEqual(
            _sanitize_stance("抗战期间（1937–1945年）日本学者曾 8 次考察并盗走部分石刻", "避雷"),
            "中性",
        )
        self.assertEqual(_sanitize_stance("石窟开凿于北魏，距今 1500 年", "避雷"), "中性")
        # 带可执行警示词的"避雷"：不降级（是真提醒）
        self.assertEqual(_sanitize_stance("明代壁画区注意别开闪光灯", "避雷"), "避雷")
        self.assertEqual(_sanitize_stance("闭馆前半小时停止入园，别卡点", "避雷"), "避雷")
        # 推荐立场不受影响（历史亮点照常进亮点清单）
        self.assertEqual(_sanitize_stance("金代彩塑被誉为美学巅峰", "推荐"), "推荐")


class TestServicePrimitives(unittest.TestCase):
    def test_cancel_primitives(self):
        """cancel_job 只对内存中 running 的任务生效；终态/不存在返回 False。"""
        import service.research as r

        with r._LOCK:
            r.JOBS["fake_running"] = {"id": "fake_running", "status": "running",
                                      "cancel_requested": False}
            r.JOBS["fake_done"] = {"id": "fake_done", "status": "done"}
        try:
            self.assertTrue(r.cancel_job("fake_running"))
            self.assertTrue(r.JOBS["fake_running"]["cancel_requested"])
            self.assertFalse(r.cancel_job("fake_done"))
            self.assertFalse(r.cancel_job("不存在"))
        finally:
            with r._LOCK:
                r.JOBS.pop("fake_running", None)
                r.JOBS.pop("fake_done", None)


class TestApi(unittest.TestCase):
    def test_endpoints_and_traversal_guard(self):
        from fastapi.testclient import TestClient

        from api_server import app

        c = TestClient(app)
        self.assertEqual(c.get("/api/health").status_code, 200)
        # history 路由必须优先于 {job_id}，否则被截胡成 404
        self.assertEqual(c.get("/api/jobs/history").status_code, 200)
        self.assertEqual(c.get("/api/jobs/不存在的任务").status_code, 404)
        self.assertEqual(c.get("/api/reports").status_code, 200)
        for evil in ("..\\..\\README.md", "../../README.md", "config.py", "not_exist.md"):
            self.assertEqual(
                c.get("/api/reports/download", params={"name": evil}).status_code,
                404,
                f"目录穿越未拦截: {evil}",
            )


class TestGeo(unittest.TestCase):
    def test_distance_km(self):
        """球面距离纯函数：北京两大坐标约 4.3km，非法输入返回 None。"""
        from core.geo import distance_km

        d = distance_km("116.391,39.907", "116.434,39.909")  # 故宫->国贸附近
        self.assertIsNotNone(d)
        self.assertTrue(3 < d < 6)
        self.assertIsNone(distance_km("bad", "116.4,39.9"))
        self.assertIsNone(distance_km(None, "116.4,39.9"))

    def test_degrade_without_key(self):
        """无高德 Key 时全部返回 None（降级路径，不抛异常）。"""
        from core import geo

        orig = geo.AMAP_API_KEY
        geo.AMAP_API_KEY = ""
        try:
            self.assertFalse(geo.available())
            self.assertIsNone(geo.geocode_poi("西湖", "杭州"))
            self.assertIsNone(geo.travel_time("116.4,39.9", "116.5,39.9"))
        finally:
            geo.AMAP_API_KEY = orig


class TestPlanner(unittest.TestCase):
    def test_normalize_profile(self):
        """档案规范化：非法时长/时段降级，字段补齐。"""
        from pipeline.planner import _normalize_profile

        p = _normalize_profile({
            "duration_hours": "2.5", "best_time_slot": "凌晨",
            "highlights": ["大佛", "  ", 123], "avoid": "不是列表",
        })
        self.assertEqual(p["duration_hours"], 2.5)
        self.assertEqual(p["best_time_slot"], "全天")  # 非法时段降级
        self.assertEqual(p["highlights"], ["大佛", "123"])
        self.assertEqual(p["avoid"], [])
        # 超范围时长与缺失字段
        p2 = _normalize_profile({"duration_hours": 99})
        self.assertIsNone(p2["duration_hours"])
        self.assertEqual(p2["tips"], [])

    def test_normalize_plan(self):
        """行程规范化：不在名单的景点丢弃，非法时段修正，天数截断。"""
        from pipeline.planner import _normalize_plan

        data = {"days": [
            {"slots": [
                {"slot": "上午", "spot": "云冈石窟", "reasons": "大佛"},
                {"slot": "半夜", "spot": "华严寺"},
                {"slot": "下午", "spot": "不存在的景点"},
            ]},
            {"slots": [{"slot": "上午", "spot": "善化寺"}]},
            {"slots": [{"slot": "上午", "spot": "华严寺"}]},  # 超出 days=2 截断
        ]}
        plan = _normalize_plan(data, {"云冈石窟", "华严寺", "善化寺"}, days=2)
        self.assertEqual(len(plan["days"]), 2)
        day1 = plan["days"][0]["slots"]
        self.assertEqual([s["spot"] for s in day1], ["云冈石窟", "华严寺"])
        self.assertEqual(day1[1]["slot"], "下午")  # 非法时段修正为默认值
        self.assertEqual(plan["days"][1]["slots"][0]["spot"], "善化寺")

    def test_render_trip(self):
        """路书渲染：逐日卡片 + 景点详情卡 + 溯源链接 + 参数回显都在。"""
        from pipeline.planner import render_trip

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "约2.5小时",
             "transport": "公交约40分钟", "reasons": "大佛震撞",
             "notes": "全程两万步", "food": ""}]}]}
        profiles = {"云冈石窟": {
            "duration_hours": 2.5, "best_time_slot": "上午",
            "highlights": ["露天大佛"], "avoid": ["两万步劝退"],
            "food": [], "photo_spots": ["第20窟"], "tips": ["早去避开旅行团"]}}
        md = render_trip("大同", 1, "大同站", plan, profiles,
                         {"云冈石窟": ["https://www.douyin.com/video/123"]}, geo_on=False)
        for expect in ("《大同》1 天行程规划", "上午 · 云冈石窟", "公交约40分钟",
                       "全程两万步", "别踩坑", "[来源1]", "LLM 交通估算",
                       "你的需求：目的地 **大同**", "调整参数后重新生成",
                       "💡 提示：全程两万步"):
            self.assertIn(expect, md)


class TestTripApi(unittest.TestCase):
    def test_trip_validation(self):
        """/api/trip 参数校验：空城市/非法天数/非法预算/非法消费偏好拒绝，不实际起任务。"""
        from fastapi.testclient import TestClient

        from api_server import app

        c = TestClient(app)
        self.assertEqual(c.post("/api/trip", json={"city": "  "}).status_code, 400)
        self.assertEqual(c.post("/api/trip", json={"city": "大同", "days": 0}).status_code, 400)
        self.assertEqual(c.post("/api/trip", json={"city": "大同", "days": 8}).status_code, 400)
        self.assertEqual(c.post("/api/trip", json={"city": "大同", "budget": -5}).status_code, 400)
        self.assertEqual(
            c.post("/api/trip", json={"city": "大同", "preference_mode": "豪华优先"}).status_code, 400)


class TestCandidates(unittest.TestCase):
    def test_marketing_regex(self):
        """营销号文案识别：强营销信号命中，普通分享不误伤。"""
        from pipeline.candidates import is_marketing

        self.assertTrue(is_marketing("点击左下角领优惠券，团购只要 99"))
        self.assertTrue(is_marketing("探店合作，粉丝福利来了"))
        self.assertFalse(is_marketing("今天去了云冈石窟，大佛太震撞了"))
        self.assertFalse(is_marketing(""))

    def test_verify_fallback(self):
        """交叉验证兜底：LLM 无输出时，有正面证据且营销号不过半则 keep。"""
        import pipeline.candidates as pc

        orig = pc.chat_json
        pc.chat_json = lambda *a, **k: {}  # 模拟 LLM 无输出
        try:
            cands = [
                {"name": "甲", "category": "景点", "reason": ""},
                {"name": "乙", "category": "美食", "reason": ""},
                {"name": "丙", "category": "景点", "reason": ""},
            ]
            stats = {
                "甲": {"videos": 5, "marketing_hits": 0, "positive": 3, "negative": 1, "sample_quotes": []},
                "乙": {"videos": 4, "marketing_hits": 4, "positive": 1, "negative": 0, "sample_quotes": []},
                "丙": {"videos": 0, "marketing_hits": 0, "positive": 0, "negative": 0, "sample_quotes": []},
            }
            results = {r["name"]: r for r in pc.verify_candidates(cands, stats)}
            self.assertEqual(results["甲"]["verdict"], "keep")   # 正面证据充足
            self.assertEqual(results["乙"]["verdict"], "drop")   # 营销号占比 100%
            self.assertEqual(results["丙"]["verdict"], "drop")   # 无任何证据
        finally:
            pc.chat_json = orig

    def test_generate_quota_trim(self):
        """候选生成：类别配额与总量上限裁剪生效。"""
        import pipeline.candidates as pc

        orig = pc.chat_json
        # 模拟 LLM 返回超量候选：景点 15 个 + 美食 8 个
        pc.chat_json = lambda *a, **k: {"candidates": (
            [{"name": f"景{i}", "category": "景点", "reason": ""} for i in range(15)]
            + [{"name": f"食{i}", "category": "美食", "reason": ""} for i in range(8)]
        )}
        try:
            picked = pc.generate_candidates("大同", 2, "")
            cats = [c["category"] for c in picked]
            self.assertLessEqual(cats.count("景点"), pc.CATEGORY_QUOTA["景点"])
            self.assertLessEqual(cats.count("美食"), pc.CATEGORY_QUOTA["美食"])
            self.assertLessEqual(len(picked), pc.TOTAL_CANDIDATES_MAX)
        finally:
            pc.chat_json = orig


class TestHeatAndPitfall(unittest.TestCase):
    def _items(self):
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        fresh = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        return [
            SimpleNamespace(like_count=20000, comments=["a"] * 40, publish_time=fresh),
            SimpleNamespace(like_count=8000, comments=["b"] * 30, publish_time=fresh),
            SimpleNamespace(like_count=100, comments=["c"], publish_time="2020-01-01"),
            SimpleNamespace(like_count=None, comments=[], publish_time="乱码"),
        ]

    def test_heat_index(self):
        """热度指数：0~1 区间，新鲜度/互动计入；空列表不崩。"""
        from pipeline.heat import heat_index

        h = heat_index(self._items())
        self.assertTrue(0 < h["score"] <= 1)
        self.assertEqual(h["videos"], 4)
        self.assertEqual(h["likes"], 28100)
        self.assertEqual(h["comments"], 71)
        self.assertEqual(h["fresh_ratio"], 0.5)
        self.assertEqual(heat_index([])["score"], 0.0)

    def test_pitfall_digest(self):
        """避坑专题：只收避雷立场，多源一致排前，上限截断。"""
        from pipeline.heat import DIGEST_MAX, pitfall_digest

        pts = [
            {"claim": "单源坑", "stance": "避雷", "confidence": "单源", "quote": "q1", "source": "u1"},
            {"claim": "多源坑", "stance": "避雷", "confidence": "多源一致", "quote": "", "source": ""},
            {"claim": "推荐项", "stance": "推荐", "confidence": "多源一致"},
        ] + [
            {"claim": f"坑{i}", "stance": "避雷", "confidence": "单源"} for i in range(20)
        ]
        rows = pitfall_digest(pts)
        self.assertEqual(len(rows), DIGEST_MAX)
        self.assertEqual(rows[0]["claim"], "多源坑")
        self.assertTrue(all(r["claim"] != "推荐项" for r in rows))


class TestBudgetAndHtml(unittest.TestCase):
    def _profiles(self):
        return {
            "云冈石窟": {
                "duration_hours": 2.5, "best_time_slot": "上午",
                "highlights": ["大佛"], "avoid": ["两万步"], "food": [],
                "photo_spots": [], "tips": [],
                "cost_items": [{"item": "门票", "type": "门票", "amount": 120.0}],
            },
            "华严寺": {
                "duration_hours": 1.5, "best_time_slot": "下午",
                "highlights": [], "avoid": [], "food": ["凤临阁人均 80"],
                "photo_spots": [], "tips": [],
                "cost_items": [{"item": "门票", "type": "门票", "amount": 50.0},
                               {"item": "午餐", "type": "餐饮人均", "amount": 80.0}],
            },
        }

    def _plan(self):
        return {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "约2.5小时", "transport": "",
             "cost": 120, "reasons": "", "notes": "", "food": "", "pitfall_quotes": ["走到勝"]},
            {"slot": "下午", "spot": "华严寺", "duration": "", "transport": "",
             "cost": 50, "reasons": "", "notes": "", "food": "", "pitfall_quotes": []},
        ]}]}

    def test_budget_summary(self):
        """预算明细：门票去重求和、结余/超支判定、弹性 10%。"""
        from pipeline.planner import build_budget_summary

        b = build_budget_summary(self._profiles(), self._plan(), days=2, budget=1500)
        self.assertEqual(b["tickets"], 170.0)          # 120 + 50
        self.assertEqual(b["food"], 80 * 2 * 2)        # 人均×2正餐×2天
        self.assertEqual(b["total_budget"], 1500.0)
        self.assertEqual(b["status"], "结余")
        self.assertAlmostEqual(b["total"], (b["tickets"] + b["food"] + b["transport"]) * 1.1)
        # 紧预算触发超支建议
        b2 = build_budget_summary(self._profiles(), self._plan(), days=7, budget=100)
        self.assertEqual(b2["status"], "超支")
        self.assertIn("超出预算", b2["note"])

    def test_render_trip_html(self):
        """HTML 渲染：关键区块（概览卡/预算图/地图/避坑/热度/免责声明）与离线降级脚本都在。"""
        from pipeline.planner import build_budget_summary, render_trip_html

        profiles, plan = self._profiles(), self._plan()
        b = build_budget_summary(profiles, plan, days=1, budget=None)
        html = render_trip_html(
            "大同", 1, "大同古城内", plan, profiles,
            {"云冈石窟": ["https://www.douyin.com/video/1"], "华严寺": []},
            geo_on=True, locs={"云冈石窟": "113.05,40.04", "华严寺": "bad", "酒店": "113.2,40.1"},
            budget_summary=b,
            pitfall=[{"claim": "两万步勝退", "quote": "走到腳断", "source": "u", "confidence": "多源一致"}],
            heat=[{"spot": "云冈石窟", "score": 0.82, "trend": "近期热度上升",
                   "videos": 5, "likes": 28100, "comments": 71}],
        )
        for expect in ("《大同》1 天行程规划", "行程概览", "budget-chart", "echarts", "leaflet",
                       "两万步勝退", "走到腳断", "近期热度上升", "信息溯源",
                       "仅供参考", '"lng": 113.05', "当日花费小计", "预算明细",
                       "timeline", "pit-card", "echo-bar", "heat-bar", "heat-note"):
            self.assertIn(expect, html)
        # 非法坐标被丢弃，合法坐标进 markers（酒店也在）
        self.assertNotIn("bad", html)

    def test_overview_subtotals_breakdown(self):
        """概览卡/每日小计/预算明细三个纯函数，及 Markdown 渲染新板块。"""
        from pipeline.planner import (
            budget_breakdown,
            build_budget_summary,
            build_overview,
            day_subtotals,
            render_trip,
        )

        profiles, plan = self._profiles(), self._plan()
        b = build_budget_summary(profiles, plan, days=2, budget=1500)
        ov = build_overview(2, plan, profiles, b, pitfall=[{"claim": "x"}])
        self.assertEqual(ov["days"], 2)
        self.assertEqual(ov["spots"], 2)
        self.assertEqual(ov["slots"], 2)
        self.assertEqual(ov["total_cost"], b["total"])
        self.assertEqual(ov["daily_cost"], round(b["total"] / 2, 2))
        self.assertEqual(ov["pitfalls"], 1)
        # 每日小计：点位花费 + 餐饮/交通日均摊（弹性不分摊）
        sc = day_subtotals(plan, b)
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0]["spots"], 170)             # 120 + 50
        self.assertEqual(sc[0]["food"], round(b["food"] / 1))
        self.assertEqual(sc[0]["total"], sc[0]["spots"] + sc[0]["food"] + sc[0]["transport"])
        # 明细行：门票逐点列明，四行固定结构；无预算时空列表
        rows = budget_breakdown(profiles, b, days=2)
        self.assertEqual([r["item"] for r in rows], ["门票", "餐饮", "市内交通", "弹性预留"])
        self.assertIn("云冈石窟", rows[0]["detail"])
        self.assertEqual(budget_breakdown(profiles, None, 2), [])
        # Markdown 渲染：概览/小计/末尾明细表都在，旧顶部简表已去掉不重复
        md = render_trip("大同", 2, "酒店", {"summary_note": "", "days": [plan["days"][0], plan["days"][0]]},
                         profiles, {}, geo_on=False, budget_summary=b)
        for expect in ("## 行程概览", "每日预算", "当日花费小计", "## 预算明细",
                       "| **预估总计**", "用户预算"):
            self.assertIn(expect, md)
        self.assertEqual(md.count("## 预算明细"), 1)  # 不重复


class TestPlanQualityGuards(unittest.TestCase):
    """北京样例 P0 修复：覆盖率兜底检查 + 预算只计已排入景点 + 缺票价明示。"""

    def test_coverage_issues(self):
        from pipeline.planner import _coverage_issues

        profiles = {
            "环球影城": {"best_time_slot": "全天"},
            "什刹海": {"best_time_slot": "晚上"},   # 夜景型，必须排晚上
            "天坛": {"best_time_slot": "上午"},
        }
        # 只排 1 个点 + 什刹海排错时段 + 未排景点无备选说明 → 三类问题都命中
        bad = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "天坛"}, {"slot": "下午", "spot": "什刹海"}]}]}
        issues = _coverage_issues(bad, profiles, days=2)
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("低于下限" in x for x in issues))
        self.assertTrue(any("什刹海" in x and "晚上" in x for x in issues))
        self.assertTrue(any("环球影城" in x for x in issues))
        # 排够点数 + 晚上型归位 + summary_note 提到备选 → 无问题
        good = {"summary_note": "天坛列为备选，时间不够", "days": [
            {"day": 1, "slots": [{"slot": "全天", "spot": "环球影城"},
                                   {"slot": "晚上", "spot": "什刹海"}]},
            {"day": 2, "slots": [{"slot": "上午", "spot": "天坛"},
                                   {"slot": "下午", "spot": "环球影城"}]}]}
        self.assertEqual(_coverage_issues(good, profiles, days=2), [])

    def test_budget_only_planned_tickets(self):
        from pipeline.planner import budget_breakdown, build_budget_summary

        profiles = {
            "环球影城": {"cost_items": [{"item": "门票", "type": "门票", "amount": 528.0}]},
            "天坛": {"cost_items": [{"item": "门票", "type": "门票", "amount": 35.0}]},
            "免费公园": {"cost_items": []},
        }
        plan = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "全天", "spot": "环球影城"}, {"slot": "晚上", "spot": "免费公园"}]}]}
        b = build_budget_summary(profiles, plan, days=1, budget=None)
        self.assertEqual(b["tickets"], 528.0)              # 天坛没排入，35 元不计
        self.assertEqual(b["tickets_missing"], ["免费公园"])  # 排入了但无票价 → 明示
        rows = budget_breakdown(profiles, b, days=1)
        self.assertIn("免费公园", rows[0]["detail"])        # 明细行提醒核实
        self.assertIn("出发前请核实", rows[0]["detail"])
        # 全部排入且都有票价 → 无 missing
        plan_all = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "环球影城"}, {"slot": "下午", "spot": "天坛"}]}]}
        b2 = build_budget_summary(profiles, plan_all, days=1, budget=None)
        self.assertEqual(b2["tickets"], 528.0 + 35.0)
        self.assertNotIn("免费公园", b2["tickets_missing"])

    def test_budget_slot_cost_fallback(self):
        """档案无门票项时用点位 cost 兜底（环球 650 在 slot.cost 不在 cost_items 的场景）。"""
        from pipeline.planner import build_budget_summary

        profiles = {"环球影城": {"cost_items": []}, "什刹海": {"cost_items": []}}
        plan = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "环球影城", "cost": 650},
            {"slot": "下午", "spot": "什刹海", "cost": 0}]}]}
        b = build_budget_summary(profiles, plan, days=1, budget=1500)
        self.assertEqual(b["tickets"], 650.0)
        self.assertIn("环球影城 点位预估 650 元", b["tickets_detail"])
        self.assertEqual(b["tickets_missing"], ["什刹海"])   # cost=0 且无档案票价 → 仍明示
        self.assertGreater(b["total"], 650.0)                # 总计不再是漏算环球的 319

    def test_filter_fabricated_food(self):
        """餐厅防编造：food 未命中候选店名则清空，命中则保留。"""
        from pipeline.planner import _filter_fabricated_food

        plan = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "下午", "spot": "什刹海", "food": "后海铜锅涮肉：白汤锅底（人均120元）"},
            {"slot": "晚上", "spot": "五道营胡同", "food": "胡大饭馆：簋街小龙虾（人均100元）"}]}]}
        out = _filter_fabricated_food(plan, {"胡大饭馆"})
        slots = out["days"][0]["slots"]
        self.assertEqual(slots[0]["food"], "")            # 编造店名被清空
        self.assertIn("胡大饭馆", slots[1]["food"])       # 候选内保留
        # 无候选时全部清空
        out2 = _filter_fabricated_food(plan, set())
        self.assertEqual([s["food"] for s in out2["days"][0]["slots"]], ["", ""])

    def test_verify_food_rescue(self):
        """美食保底：餐厅全被评审淘汰但有正面证据时，恢复证据最强的前 2 家。"""
        import pipeline.candidates as pc

        orig = pc.chat_json
        # 模拟 LLM 评审把两家餐厅都判 drop（北京任务实况），景点 keep
        pc.chat_json = lambda *a, **k: {"results": [
            {"name": "景A", "verdict": "keep", "evidence": "强", "pitfall_risk": "低", "reason": "好"},
            {"name": "食A", "verdict": "drop", "evidence": "中", "pitfall_risk": "中", "reason": "排队久"},
            {"name": "食B", "verdict": "drop", "evidence": "弱", "pitfall_risk": "中", "reason": "争议多"},
            {"name": "食C", "verdict": "drop", "evidence": "弱", "pitfall_risk": "高", "reason": "负面强"},
        ]}
        try:
            cands = [{"name": "景A", "category": "景点", "reason": ""},
                     {"name": "食A", "category": "美食", "reason": ""},
                     {"name": "食B", "category": "美食", "reason": ""},
                     {"name": "食C", "category": "美食", "reason": ""}]
            stats = {
                "景A": {"videos": 5, "marketing_hits": 0, "positive": 6, "negative": 1, "sample_quotes": []},
                "食A": {"videos": 5, "marketing_hits": 0, "positive": 8, "negative": 0, "sample_quotes": []},
                "食B": {"videos": 5, "marketing_hits": 1, "positive": 9, "negative": 3, "sample_quotes": []},
                "食C": {"videos": 4, "marketing_hits": 4, "positive": 1, "negative": 5, "sample_quotes": []},
            }
            results = {r["name"]: r for r in pc.verify_candidates(cands, stats)}
            self.assertEqual(results["食A"]["verdict"], "keep")   # 证据中+正面多 → 恢复
            self.assertEqual(results["食B"]["verdict"], "keep")   # 证据弱但正面最多 → 恢复（前 2 家）
            self.assertEqual(results["食C"]["verdict"], "drop")   # 营销号过半 → 不恢复
            self.assertIn("美食保底恢复", results["食A"]["reason"])
        finally:
            pc.chat_json = orig


class TestCrawlSpeedUp(unittest.TestCase):
    """采集提速：全局令牌桶 / 评论 JSON 解析 / 条件等待 / 多 Tab 并发调度。"""

    def test_rate_limiter_global_queue(self):
        """全局令牌桶：并发调用也排在同一条时间轴上（请求间隔不因并发缩短）。"""
        import time
        from concurrent.futures import ThreadPoolExecutor

        from core.rate_limiter import RateLimiter, global_limiter

        RateLimiter.reset()
        lim = RateLimiter(min_s=0.05, max_s=0.05)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: lim.wait(), range(4)))
        # 4 次请求：首个立即放行，后 3 个各排 0.05 秒 → 总耗时至少 0.15 秒
        self.assertGreaterEqual(time.time() - t0, 0.14)
        # 不同实例共享同一队列（多 Tab 各持一个实例也不会抢间隔）
        RateLimiter.reset()
        a, b = RateLimiter(min_s=0.05, max_s=0.05), RateLimiter(min_s=0.05, max_s=0.05)
        a.wait()
        t1 = time.time()
        b.wait()
        self.assertGreaterEqual(time.time() - t1, 0.04)
        self.assertIs(global_limiter(), global_limiter())   # 单例
        RateLimiter.reset()

    def test_timestamp_to_date(self):
        """评论精确时间戳换算：字符串数字也认，脏数据/越界给 None。"""
        import time
        from datetime import datetime

        from core.sanitize import timestamp_to_date

        ts = int(datetime(2026, 3, 5, 10, 30).timestamp())
        self.assertEqual(timestamp_to_date(ts), "2026-03-05")
        self.assertEqual(timestamp_to_date(str(ts)), "2026-03-05")
        self.assertIsNone(timestamp_to_date(None))
        self.assertIsNone(timestamp_to_date("abc"))
        self.assertIsNone(timestamp_to_date(100))                          # 早于抖音上线
        self.assertIsNone(timestamp_to_date(int(time.time()) + 10 ** 6))   # 未来太多

    def test_parse_comment_payload(self):
        """评论接口 JSON 解析：作者 UID 比对、脏数据跳过，个人信息不得落入结果。"""
        from datetime import datetime

        from crawler.douyin import parse_comment_payload

        ts = int(datetime(2026, 1, 15, 12, 0).timestamp())
        rows = parse_comment_payload([
            {"text": "门票免费的", "digg_count": 35, "create_time": ts,
             "user": {"uid": "111", "nickname": "小明", "ip_label": "北京"}},
            {"text": "作者说得对", "digg_count": 3, "create_time": ts, "user": {"uid": "999"}},
            {"text": "   ", "digg_count": 9, "user": {"uid": "1"}},      # 空正文丢弃
            "不是字典",                                                 # 脏数据跳过
            {"text": "点赞是字符串", "digg_count": "1.2万", "create_time": "abc"},
        ], author_uid="999")
        self.assertEqual([r["text"] for r in rows], ["门票免费的", "作者说得对", "点赞是字符串"])
        self.assertEqual(rows[0]["like_count"], 35)
        self.assertEqual(rows[0]["time"], "2026-01-15")     # 精确日期（情感趋势用）
        self.assertFalse(rows[0]["is_author_reply"])
        self.assertTrue(rows[1]["is_author_reply"])         # UID 命中作者
        self.assertEqual(rows[2]["like_count"], 12000)      # 字符串计数走 parse_count
        self.assertIsNone(rows[2]["time"])                  # 非法时间戳给 None
        for r in rows:   # 合规：只允许白名单四个字段，昵称/UID/属地不得进入下游
            self.assertEqual(set(r.keys()), {"text", "like_count", "is_author_reply", "time"})

    def test_packet_json_and_author_uid(self):
        """监听包容错：dict/JSON 字符串/bytes 都认，其余静默降级为 None。"""
        from types import SimpleNamespace

        from crawler.douyin import author_uid_of, packet_json

        pk = lambda body: SimpleNamespace(response=SimpleNamespace(body=body))
        self.assertEqual(packet_json(pk({"a": 1})), {"a": 1})
        self.assertEqual(packet_json(pk('{"a": 2}')), {"a": 2})
        self.assertEqual(packet_json(pk(b'{"a": 3}')), {"a": 3})
        self.assertIsNone(packet_json(pk("不是JSON")))
        self.assertIsNone(packet_json(pk([1, 2])))
        self.assertIsNone(packet_json(SimpleNamespace(response=None)))   # 取 body 报错也降级
        self.assertEqual(author_uid_of({"aweme_detail": {"author": {"uid": "u1"}}}), "u1")
        self.assertEqual(author_uid_of({"item_list": [{"author": {"sec_uid": "s2"}}]}), "s2")
        self.assertIsNone(author_uid_of({"other": 1}))

    def test_rank_and_filter_shared(self):
        """两条采集路径共用的点赞排序+低质过滤+保底放宽。"""
        from core.models import Comment
        from crawler.douyin import rank_and_filter

        cs = [Comment(text=f"h{i}", like_count=10 + i) for i in range(5)] + [
            Comment(text="low", like_count=1),
            Comment(text="author", like_count=0, is_author_reply=True)]
        out = rank_and_filter(cs, 10)
        self.assertEqual(out[0].text, "h4")                      # 高赞在前
        self.assertIn("author", [c.text for c in out])           # 作者回复豁免门槛
        self.assertNotIn("low", [c.text for c in out])           # 1 赞低质被过滤
        self.assertEqual(len(out), 6)
        self.assertEqual([c.text for c in rank_and_filter(cs, 2)], ["h4", "h3"])   # 截断
        weak = [Comment(text=str(i), like_count=0) for i in range(3)]
        self.assertEqual(len(rank_and_filter(weak, 10)), 3)      # 不足保底数则放宽

    def test_comments_by_listen(self):
        """评论监听路径：接口 JSON 包直接解析成评论，无需滚 DOM。"""
        from datetime import datetime
        from types import SimpleNamespace

        from crawler.douyin import DouyinCrawler

        ts = int(datetime(2026, 2, 10, 9, 0).timestamp())
        packets = [
            SimpleNamespace(url="https://www.douyin.com/aweme/v1/web/aweme/detail/?x=1",
                            response=SimpleNamespace(body={"aweme_detail": {"author": {"uid": "u9"}}})),
            SimpleNamespace(url="https://www.douyin.com/aweme/v1/web/comment/list/?y=1",
                            response=SimpleNamespace(body={"comments": [
                                {"text": "排队两小时", "digg_count": 88, "create_time": ts,
                                 "user": {"uid": "u1"}},
                                {"text": "周一闭馆", "digg_count": 5, "create_time": ts,
                                 "user": {"uid": "u9"}},
                                {"text": "排队两小时", "digg_count": 1, "user": {"uid": "u2"}},  # 重复正文去重
                            ]})),
        ]

        class FakeListen:
            def __init__(self): self.started = None
            def start(self, targets): self.started = targets
            def stop(self): pass
            def wait(self, count=1, timeout=None, fit_count=True, raise_err=None):
                return packets.pop(0) if packets else None

        class FakePage:
            def __init__(self): self.listen = FakeListen()
            def ele(self, sel, timeout=None): return None
            def run_js(self, *a, **k): return None

        got = DouyinCrawler(FakePage())._comments_by_listen(max_n=10, container=None)
        self.assertEqual([r["text"] for r in got], ["排队两小时", "周一闭馆"])   # 按正文去重
        self.assertEqual(got[0]["like_count"], 88)
        self.assertEqual(got[0]["time"], "2026-02-10")
        self.assertTrue(got[1]["is_author_reply"])     # 作者 UID 先于评论包到达，比对生效

    def test_wait_helpers(self):
        """条件等待：命中即返回，未命中以 timeout 封顶（不按选择器个数累加）。"""
        import time

        from crawler.douyin import _wait_any, _wait_until

        self.assertTrue(_wait_until(lambda: True, timeout=1))
        t0 = time.time()
        self.assertFalse(_wait_until(lambda: False, timeout=0.3, interval=0.05))
        self.assertGreaterEqual(time.time() - t0, 0.28)
        state = {"n": 0}

        def third_time():
            state["n"] += 1
            return state["n"] >= 3

        t1 = time.time()
        self.assertTrue(_wait_until(third_time, timeout=5, interval=0.01))
        self.assertLess(time.time() - t1, 1)          # 命中即返回，不等满上限

        class Scope:
            def ele(self, sel, timeout=None):
                return "ELE" if sel == "css:ok" else None

        self.assertEqual(_wait_any(Scope(), ["css:bad", "css:ok"], timeout=2), "ELE")

        class Empty:
            def ele(self, sel, timeout=None):
                return None

        t2 = time.time()
        self.assertIsNone(_wait_any(Empty(), ["css:a", "css:b"], timeout=0.3))
        self.assertLess(time.time() - t2, 0.55)       # 两个选择器也只等 0.3 秒，不累加

    def test_open_tabs_degrade(self):
        """Tab 池：开不出来就退化为可用数量，至少保留主页面。"""
        from crawler.tabs import close_tabs, open_tabs

        class OkPage:
            def __init__(self, n=2): self.n, self.made, self.closed = n, 0, False
            def new_tab(self):
                if self.made >= self.n:
                    raise RuntimeError("开不出更多标签页")
                self.made += 1
                return OkPage(0)
            def close(self): self.closed = True

        main = OkPage()
        tabs = open_tabs(main, 5)
        self.assertEqual(len(tabs), 3)              # 主页 + 2 个（第三个报错就停）
        self.assertIs(tabs[0], main)
        close_tabs(tabs, main)
        self.assertFalse(main.closed)               # 主页面不关
        self.assertTrue(all(t.closed for t in tabs[1:]))

        class BadPage:
            def new_tab(self): raise RuntimeError("不支持多标签")
        self.assertEqual(len(open_tabs(BadPage(), 3)), 1)

    def test_fetch_videos_parallel(self):
        """多 Tab 并发：结果按原顺序返回，且同一 Tab 任意时刻只有一个线程在驱动。"""
        import time
        from types import SimpleNamespace

        import crawler.douyin as cd
        from crawler.tabs import fetch_videos

        created, instances = [], []

        class FakeCrawler:
            def __init__(self, page, limiter):
                self.page, self.limiter, self.inside, self.max_inside = page, limiter, 0, 0
                instances.append(self)

            def fetch_video(self, url, **kw):
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
                time.sleep(0.02)
                self.inside -= 1
                return SimpleNamespace(video_id=url[-1], description="abc",
                                       comments=[1, 2], play_urls=[])

        def make_page():
            class P:
                def new_tab(self):
                    t = P()
                    created.append(t)
                    return t

                def close(self):
                    self.closed = True
            return P()

        orig = cd.DouyinCrawler
        cd.DouyinCrawler = FakeCrawler
        try:
            page = make_page()
            urls = [f"https://www.douyin.com/video/{i}" for i in range(6)]
            logs = []
            out = fetch_videos(page, urls, workers=3, log=logs.append)
            self.assertEqual(len(created), 2)                        # 主页 + 2 个新 Tab
            self.assertEqual([i for i, it, e in out], list(range(6)))  # 顺序不乱
            self.assertTrue(all(it is not None and e is None for _, it, e in out))
            self.assertEqual([it.video_id for _, it, _ in out], [u[-1] for u in urls])
            self.assertEqual(len(instances), 3)                      # 每 Tab 一个 crawler
            self.assertTrue(all(c.max_inside == 1 for c in instances))  # 同 Tab 不并发
            self.assertEqual(len(logs), 6)                           # 逐条进度日志
            self.assertIn("评论 2 条", logs[0])
            # 共享全局频控器：所有 crawler 拿的是同一个实例
            self.assertEqual(len({id(c.limiter) for c in instances}), 1)
            # workers=1 不开新 Tab，串行等价
            created.clear(), instances.clear()
            page2 = make_page()
            out2 = fetch_videos(page2, urls[:2], workers=1)
            self.assertEqual(created, [])
            self.assertEqual(len(out2), 2)
            self.assertEqual(fetch_videos(make_page(), []), [])       # 空清单安全
        finally:
            cd.DouyinCrawler = orig

    def test_fetch_videos_retry_and_cancel(self):
        """单条失败自动重试；任务取消时不再继续采集。"""
        from types import SimpleNamespace

        import crawler.douyin as cd
        from crawler.tabs import fetch_videos

        class FlakyCrawler:
            tries = {}

            def __init__(self, page, limiter): pass

            def fetch_video(self, url, **kw):
                FlakyCrawler.tries[url] = FlakyCrawler.tries.get(url, 0) + 1
                if FlakyCrawler.tries[url] == 1:
                    raise RuntimeError("渲染抖动")
                return SimpleNamespace(video_id="ok", description="d", comments=[], play_urls=[])

        class P:
            def new_tab(self): return P()
            def close(self): pass

        orig = cd.DouyinCrawler
        cd.DouyinCrawler = FlakyCrawler
        try:
            logs, errs = [], []
            out = fetch_videos(P(), ["u1"], workers=1, retries=1, log=logs.append,
                               on_error=lambda i, e, tab: errs.append(e))
            self.assertIsNotNone(out[0][1])                    # 重试后成功
            self.assertEqual(FlakyCrawler.tries["u1"], 2)
            self.assertTrue(any("稍后重试" in m for m in logs))
            self.assertEqual(errs, [])                         # 成功了就不走失败回调

            # 全程抛错 → 最终失败并回调
            class DeadCrawler:
                def __init__(self, page, limiter): pass
                def fetch_video(self, url, **kw): raise RuntimeError("挂了")

            cd.DouyinCrawler = DeadCrawler
            errs2 = []
            out2 = fetch_videos(P(), ["u2"], workers=1, retries=0,
                                on_error=lambda i, e, tab: errs2.append(str(e)))
            self.assertIsNone(out2[0][1])
            self.assertEqual(errs2, ["挂了"])

            # 取消：不开工，全部空结果
            cd.DouyinCrawler = FlakyCrawler
            out3 = fetch_videos(P(), ["a", "b"], workers=1, cancelled=lambda: True)
            self.assertTrue(all(it is None for _, it, _ in out3))
        finally:
            cd.DouyinCrawler = orig


class TestMcpAndOpenapi(unittest.TestCase):
    def test_mcp_tools_registered(self):
        """MCP 服务器注册了全套 7 个工具（不拉起服务，只验证注册表）。"""
        import asyncio

        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertEqual(
            names,
            {"check_service", "start_research", "plan_trip", "get_job_status",
             "cancel_research", "list_reports", "get_report_content",
             "get_city_heat", "refresh_city_heat"},
        )

    def test_openapi_schema(self):
        """OpenAPI 规范含全部 API 端点（供 Dify/Coze/GPTs 等平台导入）。"""
        from api_server import app

        spec = app.openapi()
        paths = set(spec["paths"].keys())
        for p in ("/api/health", "/api/research", "/api/trip", "/api/jobs/{job_id}",
                  "/api/jobs/{job_id}/cancel", "/api/jobs/history",
                  "/api/reports", "/api/reports/download",
                  "/api/heat/refresh", "/api/heat/{city}"):
            self.assertIn(p, paths)
        self.assertNotIn("/", paths)  # 网页首页不进 OpenAPI（非 API 端点）


class TestWebSearchFallback(unittest.TestCase):
    def test_web_search_degrade(self):
        """联网参数不兼容时：去参自动重试成功，降级标记置位后后续调用不再携带。"""
        from types import SimpleNamespace

        import core.llm as llm

        class ParamErr(Exception):
            pass

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kw):
                self.calls.append(kw)
                if "enable_search" in (kw.get("extra_body") or {}):
                    raise ParamErr("invalid_request_error: unsupported parameter enable_search")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))])

        class FakeClient:
            api_key = "x"
            base_url = "y"

            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake = FakeClient()
        orig_cam, orig_flag = llm._client_and_model, llm._WEB_SEARCH_UNSUPPORTED
        llm._client_and_model = lambda: (fake, "m")
        llm._WEB_SEARCH_UNSUPPORTED = False
        try:
            out = llm.chat_json("s", "u", retries=2, web_search=True)
            self.assertEqual(out, {"ok": 1})
            self.assertTrue(llm._WEB_SEARCH_UNSUPPORTED)  # 降级标记置位
            self.assertEqual(len(fake.chat.completions.calls), 2)
            self.assertIn("enable_search", fake.chat.completions.calls[0]["extra_body"])
            self.assertNotIn("enable_search", fake.chat.completions.calls[1]["extra_body"])
            # 后续调用不再尝试联网参数（不重复碰壁）
            llm.chat_json("s", "u", retries=1, web_search=True)
            self.assertNotIn("enable_search", fake.chat.completions.calls[2]["extra_body"])
        finally:
            llm._client_and_model, llm._WEB_SEARCH_UNSUPPORTED = orig_cam, orig_flag

    def test_extra_body_keys(self):
        """联网参数同时携带两系兼容键；未开启时只有思考控制。"""
        from core.llm import _extra_body

        body = _extra_body(False, True)
        self.assertTrue(body["enable_search"])
        self.assertEqual(body["search"], {"enable": True})
        self.assertEqual(_extra_body(False, False), {"enable_thinking": False})


class TestHeatTrend(unittest.TestCase):
    def test_time_windows(self):
        """三时间窗占比：新/中/旧正确分桶，发布时间缺失计入旧内容。"""
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        from pipeline.heat import time_windows

        now = datetime.now()
        items = [
            SimpleNamespace(publish_time=(now - timedelta(days=2)).strftime("%Y-%m-%d")),
            SimpleNamespace(publish_time=(now - timedelta(days=30)).strftime("%Y-%m-%d")),
            SimpleNamespace(publish_time=(now - timedelta(days=200)).strftime("%Y-%m-%d")),
            SimpleNamespace(publish_time=None),
        ]
        w = time_windows(items, now=now)
        self.assertEqual(w, {"fresh7": 0.25, "fresh60": 0.25, "old60": 0.5})
        self.assertEqual(time_windows([]), {"fresh7": 0.0, "fresh60": 0.0, "old60": 0.0})

    def test_trend_of(self):
        """四态判定：本周最火看新鲜度，高赞旧内容标长盛不衰而非最火。"""
        from pipeline.heat import trend_of

        self.assertEqual(trend_of(0.4, 0.1, 0.2), "本周最火")   # 新内容占比高（不看分数）
        self.assertEqual(trend_of(0.0, 0.2, 0.7), "长盛不衰")   # 热度高但无新增：不是"本周最火"
        self.assertEqual(trend_of(0.0, 0.8, 0.3), "正在降温")   # 旧内容主导且热度不高
        self.assertEqual(trend_of(0.0, 0.8, 0.7), "长盛不衰")   # 高分缓解降温判定（一直火）
        self.assertEqual(trend_of(0.2, 0.3, 0.4), "平稳")


class TestDigestAndHeatApi(unittest.TestCase):
    def test_digest_quote_guard(self):
        """评价摘要引文防幻觉：不在输入原文池中的引用被丢弃，最多保留 2 条。"""
        from pipeline.planner import _normalize_digest

        pool = ["真的很好玩就是人太多了", "门票 120 有点贵"]
        out = _normalize_digest(
            {"verdict": "口碑两极", "positive": "景观震撼",
             "negative": "人太多", "quotes": [
                 "真的很好玩就是人太多了，值得去",   # 与池内原文互含，保留
                 "完全是编造的引用内容",             # 不在池中，丢弃
                 "门票 120 有点贵"]},
            pool,
        )
        self.assertEqual(out["verdict"], "口碑两极")
        self.assertEqual(len(out["quotes"]), 2)
        self.assertNotIn("完全是编造的引用内容", out["quotes"])

    def test_heat_endpoints(self):
        """/api/heat/* ：空城市拒绝 400；无快照城市返回空榜与引导提示。"""
        from fastapi.testclient import TestClient

        from api_server import app

        c = TestClient(app)
        self.assertEqual(c.post("/api/heat/refresh", json={"city": "  "}).status_code, 400)
        r = c.get("/api/heat/不存在这种城市名")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ranking"], [])
        self.assertTrue(r.json()["hint"])

    def test_heat_snapshot_roundtrip(self):
        """热度快照 UPSERT 与城市关联登记（临时库）。"""
        from core import knowledge

        tmp = Path(tempfile.mkdtemp()) / "test_heat.db"
        orig = knowledge._DB_PATH
        knowledge._DB_PATH = tmp
        try:
            knowledge.register_city_spots("大同", ["云冈石窟", "华严寺", ""])
            self.assertEqual(sorted(knowledge.list_city_spots("大同")), ["云冈石窟", "华严寺"])
            self.assertEqual(knowledge.list_city_spots("未登记"), [])
            snap = {"score": 0.5, "fresh7": 0.25, "fresh60": 0.5, "old60": 0.25,
                    "likes": 100, "videos": 4, "trend": "平稳",
                    "mkt_ratio": 0.25, "sentiment": "口碑平稳"}
            knowledge.upsert_heat_snapshot("大同", "云冈石窟", snap)
            knowledge.upsert_heat_snapshot("大同", "云冈石窟", {**snap, "score": 0.9, "trend": "本周最火",
                                                          "mkt_ratio": 0.5, "sentiment": "好评下降"})
            rows = knowledge.load_heat_snapshots("大同")
            self.assertEqual(len(rows), 1)  # UPSERT 不重复
            self.assertEqual(rows[0]["score"], 0.9)
            self.assertEqual(rows[0]["trend"], "本周最火")
            self.assertEqual(rows[0]["mkt_ratio"], 0.5)
            self.assertEqual(rows[0]["sentiment"], "好评下降")
        finally:
            knowledge._DB_PATH = orig


class TestNotesSplit(unittest.TestCase):
    def test_classify_note(self):
        """注意事项关键词兕底分类：避坑 > 费用 > 时间 > 提示。"""
        from pipeline.planner import classify_note

        self.assertEqual(classify_note("别开闪光灯，注意保护壁画"), "避坑")
        self.assertEqual(classify_note("门票120元，学生半价"), "费用")
        self.assertEqual(classify_note("8:30开放，周一闭馆"), "时间")
        self.assertEqual(classify_note("东门人少"), "提示")

    def test_normalize_notes(self):
        """notes 规范化：旧字符串拆分、非法类型兕底、None 安全。"""
        from pipeline.planner import normalize_notes

        out = normalize_notes("别排队太久；门票120元")
        self.assertEqual([x["type"] for x in out], ["避坑", "费用"])
        out = normalize_notes([{"type": "费用", "text": "人均50"}, {"type": "瞎写", "text": "周一闭馆"}])
        self.assertEqual(out[0]["type"], "费用")
        self.assertEqual(out[1]["type"], "时间")  # 非法类型按关键词兕底
        self.assertEqual(normalize_notes(None), [])

    def test_render_notes_bullets(self):
        """MD 渲染：注意事项分点，避坑/费用加粗，普通提示不加粗。"""
        from pipeline.planner import render_trip

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "", "transport": "",
             "reasons": "", "notes": "别穿高跟鞋；门票120元；早去人少", "food": ""}]}]}
        profiles = {"云冈石窟": {
            "duration_hours": None, "best_time_slot": "全天", "highlights": [],
            "avoid": [], "food": [], "photo_spots": [], "tips": [], "cost_items": []}}
        md = render_trip("大同", 1, "", plan, profiles, {}, geo_on=False)
        self.assertIn("❌ **避坑：别穿高跟鞋**", md)
        self.assertIn("💰 **费用：门票120元**", md)
        self.assertIn("💡 提示：早去人少", md)
        self.assertNotIn("**提示：早去人少**", md)


class TestSentiment(unittest.TestCase):
    def test_time_token_to_date(self):
        """评论时间换算：相对/绝对格式都认，识别不了给 None。"""
        from datetime import datetime

        from core.sanitize import time_token_to_date

        now = datetime(2026, 9, 3)
        self.assertEqual(time_token_to_date("刚刚", now), "2026-09-03")
        self.assertEqual(time_token_to_date("昨天", now), "2026-09-02")
        self.assertEqual(time_token_to_date("3天前", now), "2026-08-31")
        self.assertEqual(time_token_to_date("2周前", now), "2026-08-20")
        self.assertEqual(time_token_to_date("08-15", now), "2026-08-15")
        self.assertEqual(time_token_to_date("2025-07-01", now), "2025-07-01")
        self.assertEqual(time_token_to_date("2025年7月1日", now), "2025-07-01")
        self.assertIsNone(time_token_to_date("瞎写", now))

    def test_classify_comment(self):
        """关键词情感判定：负面优先（反词包含关系不被误判）。"""
        from pipeline.heat import classify_comment

        self.assertEqual(classify_comment("太好看了，值得二刷"), 1)
        self.assertEqual(classify_comment("不好吃，太贵了"), -1)  # 含"好吃"但先命中"不好"
        self.assertEqual(classify_comment("不推荐，纯智商税"), -1)
        self.assertEqual(classify_comment("从北京坐高铁过来的"), 0)

    def test_sentiment_trend(self):
        """情感趋势：近30天好评率对比更早，样本不足/无时间给数据不足。"""
        from datetime import datetime, timedelta

        from pipeline.heat import sentiment_trend

        now = datetime(2026, 9, 3)
        recent = (now - timedelta(days=5)).strftime("%Y-%m-%d")
        earlier = (now - timedelta(days=60)).strftime("%Y-%m-%d")

        def c(text, t):
            return {"text": text, "time": t}

        up = ([c("太难吃了", earlier)] * 4 + [c("好吃", earlier)]
              + [c("好吃", recent)] * 4 + [c("难吃", recent)])
        self.assertEqual(sentiment_trend(up, now)["trend"], "好评上升")
        down = ([c("好吃", earlier)] * 4 + [c("难吃", earlier)]
                + [c("难吃", recent)] * 4 + [c("好吃", recent)])
        self.assertEqual(sentiment_trend(down, now)["trend"], "好评下降")
        flat = [c("好吃", earlier)] * 5 + [c("好吃", recent)] * 5
        self.assertEqual(sentiment_trend(flat, now)["trend"], "口碑平稳")
        self.assertEqual(sentiment_trend([c("好吃", recent)] * 3, now)["trend"], "数据不足")
        # 旧缓存无时间字段：全部跳过 → 数据不足（不阻断榜单）
        self.assertEqual(sentiment_trend([{"text": "好吃", "time": None}] * 20, now)["trend"], "数据不足")


class TestConfidenceScoring(unittest.TestCase):
    def test_levels_and_marketing(self):
        """量化置信度：≥3源无矛盾=高，2源=中，矛盾=中，营销号来源=低且不计印证。"""
        import pipeline.verify as v

        def pt(src):
            return {"topic": "门票", "claim": "免费", "stance": "中性",
                    "time_sensitive": False, "source": src}

        orig = v.chat_json
        try:
            v.chat_json = lambda *a, **k: {"groups": [[1, 2, 3, 4]], "conflicts": []}
            out = v.annotate_confidence([pt("u1"), pt("u2"), pt("u3"), pt("mkt1")],
                                        marketing_sources={"mkt1"})
            self.assertEqual(out[0]["conf_level"], "高置信度")  # 3 个有效源无矛盾
            self.assertEqual(out[0]["conf_score"], 0.9)
            self.assertEqual(out[0]["n_sources"], 3)  # 营销号不计入印证
            self.assertEqual(out[3]["conf_level"], "低置信度")  # 自身来源是营销号
            v.chat_json = lambda *a, **k: {"groups": [[1, 2]], "conflicts": []}
            out2 = v.annotate_confidence([pt("u1"), pt("u2")])
            self.assertEqual(out2[0]["conf_level"], "中置信度")  # 2 源
            v.chat_json = lambda *a, **k: {"groups": [[1, 2, 3]], "conflicts": [[1, 2]]}
            out3 = v.annotate_confidence([pt("u1"), pt("u2"), pt("u3")])
            self.assertEqual(out3[0]["confidence"], "存分歧")
            self.assertEqual(out3[0]["conf_level"], "中置信度")  # 轻微矛盾不高于中
            self.assertEqual(out3[2]["conf_level"], "高置信度")  # 未卷入矛盾的第三源
        finally:
            v.chat_json = orig

    def test_pitfall_sorted_by_score(self):
        """避坑专题按置信度评分降序，无评分的旧数据按标签回退。"""
        from pipeline.heat import pitfall_digest

        pts = [
            {"claim": "低分坑", "stance": "避雷", "confidence": "单源",
             "conf_score": 0.3, "conf_level": "低置信度", "n_sources": 1},
            {"claim": "高分坑", "stance": "避雷", "confidence": "多源一致",
             "conf_score": 0.9, "conf_level": "高置信度", "n_sources": 3},
            {"claim": "旧数据坑", "stance": "避雷", "confidence": "多源一致"},
        ]
        rows = pitfall_digest(pts)
        self.assertEqual([r["claim"] for r in rows], ["高分坑", "旧数据坑", "低分坑"])
        self.assertEqual(rows[0]["conf_level"], "高置信度")
        self.assertEqual(rows[0]["n_sources"], 3)


class TestTransportHints(unittest.TestCase):
    def test_hints_format_and_fail(self):
        """LLM 交通估算：格式化带估算标注，缺字段丢弃，异常/空清单不阻断。"""
        import pipeline.planner as pp

        orig = pp.chat_json
        try:
            pp.chat_json = lambda *a, **k: {"routes": [
                {"from": "酒店", "to": "云冈石窟", "advice": "打车约40元/约40分钟"},
                {"from": "", "to": "x", "advice": "y"},
            ]}
            out = pp.transport_hints("大同", "古城内", ["云冈石窟"])
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0], "酒店->云冈石窟: 打车约40元/约40分钟（估算，以地图App为准）")

            def boom(*a, **k):
                raise RuntimeError("x")

            pp.chat_json = boom
            self.assertEqual(pp.transport_hints("大同", "", ["甲"]), [])
            self.assertEqual(pp.transport_hints("大同", "", []), [])
        finally:
            pp.chat_json = orig

    def test_route_advice_degrade(self):
        """无高德 Key 时 route_advice 返回 None（降级不阻断）。"""
        from core import geo

        self.assertIsNone(geo.route_advice("113,40", "114,41", "大同"))


class TestFoodsRender(unittest.TestCase):
    def test_render_foods_and_echo(self):
        """餐厅详情卡/每日餐饮推荐/概览卡餐厅数/预算餐厅人均/参数回显全链路。"""
        from pipeline.planner import build_budget_summary, render_trip

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "", "transport": "", "cost": 120,
             "reasons": "", "notes": "", "food": "凤临阁：烧麦（人均80元）"}]}]}
        profiles = {"云冈石窟": {
            "duration_hours": None, "best_time_slot": "全天", "highlights": [],
            "avoid": [], "food": [], "photo_spots": [], "tips": [],
            "cost_items": [{"item": "门票", "type": "门票", "amount": 120.0}]}}
        foods = {"凤临阁": {
            "duration_hours": None, "best_time_slot": "全天", "highlights": ["百花烧麦"],
            "avoid": ["饭点排队久"], "food": [], "photo_spots": [], "tips": [],
            "cost_items": [{"item": "人均", "type": "餐饮人均", "amount": 80.0}]}}
        b = build_budget_summary({**profiles, **foods}, plan, days=1, budget=1500)
        md = render_trip("大同", 1, "古城内", plan, profiles, {}, geo_on=False,
                         budget_summary=b, foods=foods,
                         food_sources={"凤临阁": ["https://www.douyin.com/video/9"]},
                         preferences="喜欢历史", preference_mode="均衡",
                         user_spots=["云冈石窟"])
        self.assertIn("凤临阁：烧麦（人均80元）", md)          # 每日餐饮推荐
        self.assertIn("## 餐厅详情卡（含避坑分析）", md)
        self.assertIn("百花烧麦", md)
        self.assertIn("推荐餐厅 **1 家**", md)                 # 概览卡
        self.assertIn("调研到的餐厅人均：凤临阁 人均 80 元", md)  # 预算明细依据
        self.assertIn("特别偏好 **喜欢历史**", md)             # 参数回显
        self.assertIn("指定景点 **云冈石窟**", md)

    def test_heat_index_marketing(self):
        """热度画像附带营销号计数与占比。"""
        from pipeline.heat import heat_index

        class FakeItem:
            def __init__(self, desc):
                self.description, self.like_count, self.publish_time, self.comments = desc, 10, None, []

        h = heat_index([FakeItem("点击左下角团购"), FakeItem("真实分享")])
        self.assertEqual(h["marketing"], 1)
        self.assertEqual(h["mkt_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
