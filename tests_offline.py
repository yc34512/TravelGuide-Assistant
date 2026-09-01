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
        text, like, is_author = parse_comment_block(normal)
        self.assertEqual(text, "门票免费的")
        self.assertEqual(like, 35)
        self.assertFalse(is_author)

        author = "某网友\n作者\n...\n谢谢大家支持\n2小时前·浙江\n3\n分享\n回复"
        text, like, is_author = parse_comment_block(author)
        self.assertEqual(text, "谢谢大家支持")
        self.assertTrue(is_author)

        # 宁漏不误标：正文里含"作者"二字但不是独立标记行，不认定作者回复
        tricky = "昵称\n...\n作者是好人\n1天前\n5\n分享\n回复"
        text, like, is_author = parse_comment_block(tricky)
        self.assertEqual(text, "作者是好人")
        self.assertFalse(is_author)

        # 空块
        self.assertEqual(parse_comment_block(""), ("", None, False))


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
        """路书渲染：逐日卡片 + 景点详情卡 + 溯源链接都在。"""
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
                       "全程两万步", "别踩坑", "[来源1]", "LLM 区域推断"):
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
        """HTML 渲染：关键区块（预算图/地图/避坑/热度/免责声明）与离线降级脚本都在。"""
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
        for expect in ("《大同》1 天行程规划", "budget-chart", "echarts", "leaflet",
                       "两万步勝退", "走到腳断", "近期热度上升", "信息溯源",
                       "仅供参考", '"lng": 113.05'):
            self.assertIn(expect, html)
        # 非法坐标被丢弃，合法坐标进 markers（酒店也在）
        self.assertNotIn("bad", html)


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
        """三态判定：本周最火 / 正在降温 / 平稳。"""
        from pipeline.heat import trend_of

        self.assertEqual(trend_of(0.4, 0.1, 0.2), "本周最火")   # 新内容占比高
        self.assertEqual(trend_of(0.0, 0.2, 0.7), "本周最火")   # 综合分高也算
        self.assertEqual(trend_of(0.0, 0.8, 0.3), "正在降温")   # 旧内容主导且无新增
        self.assertEqual(trend_of(0.2, 0.3, 0.4), "平稳")
        self.assertEqual(trend_of(0.0, 0.8, 0.7), "本周最火")   # 热度高优先于降温判定


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
                    "likes": 100, "videos": 4, "trend": "平稳"}
            knowledge.upsert_heat_snapshot("大同", "云冈石窟", snap)
            knowledge.upsert_heat_snapshot("大同", "云冈石窟", {**snap, "score": 0.9, "trend": "本周最火"})
            rows = knowledge.load_heat_snapshots("大同")
            self.assertEqual(len(rows), 1)  # UPSERT 不重复
            self.assertEqual(rows[0]["score"], 0.9)
            self.assertEqual(rows[0]["trend"], "本周最火")
        finally:
            knowledge._DB_PATH = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
