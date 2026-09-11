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

import config as _cfg
from core import geo as _geo

# —— 离线铁律：本套件不得发任何真实网络请求、不得消耗任何配额 ——
# .env 配了 AMAP_API_KEY 时 core.geo 会真发 HTTP（耗日配额、结果随网络漂移，
# "无 Key 降级"类用例直接失败——已踩过）。geo 是 from config import 的值拷贝，
# 所以两处都要清。需要 Key 的用例（如 TestAmapQuota）在自己 setUp 里注入 fake，不受影响。
_cfg.AMAP_API_KEY = ""
_geo.AMAP_API_KEY = ""


class TestKnowledge(unittest.TestCase):
    def test_normalize(self):
        from core.knowledge import normalize_keyword

        self.assertEqual(normalize_keyword("西湖攻略"), "西湖")
        self.assertEqual(normalize_keyword("西湖 攻略"), "西湖")
        self.assertEqual(normalize_keyword("武功山旅游攻略"), "武功山")
        self.assertEqual(normalize_keyword("攻略"), "攻略")  # 归一化后为空保留原串

    def test_normalize_strips_qualifiers(self):
        """括号限定词剥离（全角/半角）：让带分店/范围说明的候选名归一到干净键。"""
        from core.knowledge import normalize_keyword, strip_qualifiers

        self.assertEqual(strip_qualifiers("什刹海（含前海、后海、西海）"), "什刹海")
        self.assertEqual(strip_qualifiers("四季民福(故宫店)"), "四季民福")
        self.assertEqual(normalize_keyword("什刹海（含前海、后海、西海）"), "什刹海")
        self.assertEqual(normalize_keyword("西湖（一日游）攻略"), "西湖")
        self.assertEqual(strip_qualifiers("（无主名）"), "（无主名）")  # 剥空回退原串

    def test_find_fresh_reuse_and_poison_guard(self):
        """缓存复用：括号变体命中干净键 + 前缀回退命中变体名 + 空采集不算命中（防投毒）。"""
        from core import knowledge

        tmp_db = Path(tempfile.mkdtemp()) / "test_fresh.db"
        raw_dir = Path(tempfile.mkdtemp())
        orig = knowledge._DB_PATH
        knowledge._DB_PATH = tmp_db
        try:
            f1 = raw_dir / "a.json"; f1.write_text("[]", encoding="utf-8")
            knowledge.record_crawl("什刹海", str(f1), 5, 80)
            # 括号变体查询 -> 剥括号后命中干净键
            hit = knowledge.find_fresh("什刹海（含前海、后海、西海）", 7)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["video_count"], 5)

            f2 = raw_dir / "b.json"; f2.write_text("[]", encoding="utf-8")
            knowledge.record_crawl("四季民福烤鸭店", str(f2), 5, 100)
            # 前缀回退："四季民福（故宫店）"→归一"四季民福"→LIKE命中"四季民福烤鸭店"
            hit2 = knowledge.find_fresh("四季民福（故宫店）", 7)
            self.assertIsNotNone(hit2)
            self.assertEqual(hit2["video_count"], 5)

            f3 = raw_dir / "c.json"; f3.write_text("[]", encoding="utf-8")
            knowledge.record_crawl("某冷门点", str(f3), 0, 0)
            # 空采集（video_count=0）不算保鲜命中：杜绝历史空数据被误复用
            self.assertIsNone(knowledge.find_fresh("某冷门点", 7))
        finally:
            knowledge._DB_PATH = orig

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
        """路书渲染（新 TripPlan 渲染器）：逐日卡片 + 详情卡 + 溯源链接 + 参数回显都在。"""
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.trip_render import render_markdown

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "约2.5小时",
             "transport": "公交约40分钟", "reasons": "大佛震撞",
             "notes": "全程两万步", "food": ""}]}]}
        profiles = {"云冈石窟": {
            "duration_hours": 2.5, "best_time_slot": "上午",
            "highlights": ["露天大佛"], "avoid": ["两万步劝退"],
            "food": [], "photo_spots": ["第20窟"], "tips": ["早去避开旅行团"]}}
        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan,
                               sources_by_spot={"云冈石窟": ["https://www.douyin.com/video/123"]})
        tp = build_trip_plan(meta={"city": "大同", "days": 1, "stay": "大同站"},
                             decisions=decs, plan=plan,
                             snap={"profiles": profiles, "foods": {}, "heat": []}).to_dict()
        md = render_markdown(tp)
        for expect in ("《大同》1 天行程规划", "上午 · 云冈石窟", "游玩时长：约2.5小时",
                       "交通：公交约40分钟", "值得去：大佛震撞", "💡 提示：全程两万步",
                       "别踩坑：两万步劝退", "[来源1]", "LLM 交通估算",
                       "你的需求：目的地 **大同**", "住宿 **大同站**",
                       "调整参数后重新生成", "打卡点：第20窟", "贴士：早去避开旅行团"):
            self.assertIn(expect, md)

    def test_render_slot_price_same_source(self):
        """预算退役：行程槽位只展示 catalog 同源确定票价，无价时不出价格行、不出任何估算。"""
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.trip_render import render_html, render_markdown

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "故宫", "duration": "", "transport": "",
             "reasons": "", "notes": [], "food": ""},
            {"slot": "下午", "spot": "三星堆博物馆", "duration": "", "transport": "",
             "reasons": "", "notes": [], "food": ""}]}]}
        profiles = {
            "故宫": {"duration_hours": 3, "best_time_slot": "全天", "highlights": [], "avoid": [],
                   "food": [], "photo_spots": [], "tips": [],
                   "cost_items": [{"item": "门票", "type": "门票", "amount": 60.0}]},
            "三星堆博物馆": {"duration_hours": 4, "best_time_slot": "全天", "highlights": [],
                     "avoid": ["景区内的烤肠卖12元一根"], "food": [], "photo_spots": [],
                     "tips": [], "cost_items": []},
        }
        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan)
        tp = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan).to_dict()
        md = render_markdown(tp)
        self.assertIn("- 门票：60 元（同源详情）", md)     # 槽位价与详情卡同源
        self.assertNotIn("预估", md)                       # 任何金额估算口径均已退役
        self.assertIn("- 门票：待核实", md)                # 详情卡对无价点仍诚实标待核实
        # HTML 出口同一口径（模板只读 cost_label，不自算）
        html = render_html(tp)
        self.assertIn("门票 60 元", html)
        self.assertNotIn("预估", html)


class TestTripApi(unittest.TestCase):
    def test_trip_validation(self):
        """/api/trip 参数校验：空城市/非法天数/非法消费偏好拒绝，不实际起任务。"""
        from fastapi.testclient import TestClient

        from api_server import app

        c = TestClient(app)
        self.assertEqual(c.post("/api/trip", json={"city": "  "}).status_code, 400)
        self.assertEqual(c.post("/api/trip", json={"city": "大同", "days": 0}).status_code, 400)
        self.assertEqual(c.post("/api/trip", json={"city": "大同", "days": 8}).status_code, 400)
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

    def test_select_verify_fair_quota(self):
        """F2.1 公平截断：按类别配额挑选，美食/体验/购物不因返回顺序靠后被整体截断。"""
        from pipeline.candidates import VERIFY_MAX, select_verify_candidates

        cands = (
            [{"name": f"景{i}", "category": "景点"} for i in range(10)]
            + [{"name": f"食{i}", "category": "美食"} for i in range(5)]
            + [{"name": f"体{i}", "category": "体验"} for i in range(3)]
            + [{"name": f"购{i}", "category": "购物"} for i in range(2)]
        )
        picked = select_verify_candidates(cands, VERIFY_MAX)
        cats = [c["category"] for c in picked]
        self.assertEqual(len(picked), VERIFY_MAX)
        self.assertGreaterEqual(cats.count("美食"), 3)   # 美食保底进验证
        self.assertGreaterEqual(cats.count("体验"), 1)
        self.assertGreaterEqual(cats.count("购物"), 1)
        self.assertLessEqual(cats.count("景点"), 7)      # 景点不再独占前 12

    def test_select_verify_topup_when_sparse(self):
        """某类候选不足时余量按景点优先补给；候选总数不足则全取。"""
        from pipeline.candidates import select_verify_candidates

        only_spots = [{"name": f"景{i}", "category": "景点"} for i in range(10)]
        picked = select_verify_candidates(only_spots, 12)
        self.assertEqual(len(picked), 10)
        self.assertTrue(all(c["category"] == "景点" for c in picked))


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


class TestOverviewAndHtml(unittest.TestCase):
    """概览卡纯函数 + MD/HTML 双渲染（预算估算与地图分布区块已退役，只留调研到的确定事实价）。"""

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
             "reasons": "", "notes": "", "food": "", "pitfall_quotes": ["走到勝"]},
            {"slot": "下午", "spot": "华严寺", "duration": "", "transport": "",
             "reasons": "", "notes": "", "food": "", "pitfall_quotes": []},
        ]}]}

    def test_render_trip_html(self):
        """HTML 渲染（新 TripPlan 投影）：关键区块（概览卡/避坑/热度/免责）与下钻锚点都在。"""
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.planner import build_overview
        from pipeline.trip_render import render_html

        profiles, plan = self._profiles(), self._plan()
        pitfall = [{"claim": "两万步勝退", "quote": "走到腳断", "source": "u", "confidence": "多源一致"}]
        heat = [{"spot": "云冈石窟", "score": 0.82, "trend": "近期热度上升",
                 "videos": 5, "likes": 28100, "comments": 71}]
        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan,
                               sources_by_spot={"云冈石窟": ["https://www.douyin.com/video/1"]})
        snap = {"overview": build_overview(1, plan, profiles, pitfall),
                "pitfall": pitfall, "digests": {}, "legs": [],
                "dedupe_note": "已剔除重复排入的点位：华严寺（每个景点全程只排一次）",
                "rebalance_note": "已均衡排布：第1天 云冈石窟 → 第2天上午（避免某些天排得太空/太满）",
                "profiles": profiles, "foods": {}, "heat": heat}
        tp = build_trip_plan(meta={"city": "大同", "days": 1, "stay": "大同古城内"},
                             decisions=decs, plan=plan, snap=snap).to_dict()
        html = render_html(tp)
        for expect in ("《大同》1 天行程规划", "行程概览",
                       "两万步勝退", "走到腳断", "近期热度上升", "信息溯源",
                       "仅供参考", "timeline", "pit-card", "echo-bar", "heat-bar", "heat-note",
                       'id="detail-云冈石窟"', '#detail-云冈石窟',
                       "已剔除重复排入的点位：华严寺", "已均衡排布：第1天"):
            self.assertIn(expect, html)
        # 预算/地图区块已退役：不得再出现图表脚本、金额估算或坐标芯片
        for gone in ("budget-chart", "echarts", "leaflet", "景点分布", "预算明细",
                     "当日花费小计", "预估", "lng"):
            self.assertNotIn(gone, html)

    def test_overview_and_render_sections(self):
        """概览卡纯函数（新签名：不含任何金额键）及 Markdown 渲染新板块。"""
        from pipeline.planner import build_overview

        profiles, plan = self._profiles(), self._plan()
        ov = build_overview(2, plan, profiles, pitfall=[{"claim": "x"}],
                            foods={"凤临阁": {}})
        self.assertEqual(ov["days"], 2)
        self.assertEqual(ov["spots"], 2)
        self.assertEqual(ov["slots"], 2)
        self.assertEqual(ov["foods"], 1)
        self.assertEqual(ov["pitfalls"], 1)
        self.assertNotIn("total_cost", ov)          # 预算彻底退场：概览不再算钱
        self.assertNotIn("daily_cost", ov)
        self.assertEqual(build_overview(1, {"days": []}, {}, None)["slots"], 0)  # 空入参不崩
        # Markdown 渲染（新 TripPlan 渲染器）：概览/分段交通在，预算板块不在了
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.trip_render import render_markdown

        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan)
        tp = build_trip_plan(meta={"city": "大同", "days": 2}, decisions=decs, plan=plan,
                             snap={"overview": build_overview(2, plan, profiles, []),
                                   "dedupe_note": "已剔除重复排入的点位：华严寺（每个景点全程只排一次）",
                                   "rebalance_note": "已均衡排布：第1天 云冈石窟 → 第2天上午"}).to_dict()
        md = render_markdown(tp)
        for expect in ("## 行程概览", "总天数 **2 天**", "景点 **2 个**", "行程点 **2 个**",
                       "已剔除重复排入的点位：华严寺", "已均衡排布：第1天"):
            self.assertIn(expect, md)
        for gone in ("## 预算明细", "预估总计", "用户预算", "当日花费小计", "每日预算"):
            self.assertNotIn(gone, md)


class TestPlanQualityGuards(unittest.TestCase):
    """北京样例 P0 修复：覆盖率兜底 + 排布均衡兜底 + 门票唯一口径（预算模块退役，只留事实价提取）。"""

    def test_coverage_issues(self):
        from pipeline.planner import _coverage_issues

        profiles = {
            "环球影城": {"best_time_slot": "全天"},
            "什刹海": {"best_time_slot": "晚上"},   # 夜景型，必须排晚上
            "天坛": {"best_time_slot": "上午"},
        }
        # 只排 1 个点 + 什刹海排错时段 + 未排景点无备选说明 + 要 2 天只给 1 天 → 四类问题都命中
        bad = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "天坛"}, {"slot": "下午", "spot": "什刹海"}]}]}
        issues = _coverage_issues(bad, profiles, days=2)
        self.assertEqual(len(issues), 4)
        self.assertTrue(any("低于下限" in x for x in issues))
        self.assertTrue(any("什刹海" in x and "晚上" in x for x in issues))
        self.assertTrue(any("环球影城" in x for x in issues))
        self.assertTrue(any("行程天数不完整" in x for x in issues))   # P0 新增的缺天检查
        # 排够点数 + 晚上型归位 + summary_note 提到备选 → 无问题
        good = {"summary_note": "天坛列为备选，时间不够", "days": [
            {"day": 1, "slots": [{"slot": "全天", "spot": "环球影城"},
                                   {"slot": "晚上", "spot": "什刹海"}]},
            {"day": 2, "slots": [{"slot": "上午", "spot": "天坛"},
                                   {"slot": "下午", "spot": "环球影城"}]}]}
        self.assertEqual(_coverage_issues(good, profiles, days=2), [])

    def test_rebalance_days_thin_day_fixed(self):
        """排布兜底：某天只剩一个时段时，把过满天的点位搬过来；搬移不增删点位、不碰晚上型与全天型。"""
        from pipeline.planner import rebalance_days

        profiles = {
            "甲": {"best_time_slot": "上午", "duration_hours": 2},
            "乙": {"best_time_slot": "下午", "duration_hours": 2},
            "丙": {"best_time_slot": "上午", "duration_hours": 2},
            "丁": {"best_time_slot": "下午", "duration_hours": 2},
        }
        thin = {"days": [
            {"day": 1, "slots": [{"slot": "上午", "spot": "甲"}, {"slot": "下午", "spot": "乙"},
                                  {"slot": "晚上", "spot": "丙"}]},
            {"day": 2, "slots": [{"slot": "上午", "spot": "丁"}]},
        ]}
        out = rebalance_days(thin, profiles, 2)
        self.assertEqual(len(out["days"][1]["slots"]), 2)          # “第2天只有上午”被修复
        self.assertTrue(out["moved"])                              # 搬移明示给报告
        self.assertEqual(sorted(s["spot"] for d in out["days"] for s in d["slots"]),
                         ["丁", "丙", "乙", "甲"])                  # 点位不增不减
        self.assertEqual(len(thin["days"][0]["slots"]), 3)          # 纯函数不改入参
        self.assertNotIn("moved", thin)
        # 素材不够摊（3 天 2 点）→ 不硬搬
        scarce = {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "甲"}]},
                           {"day": 2, "slots": [{"slot": "下午", "spot": "乙"}]},
                           {"day": 3, "slots": []}]}
        self.assertEqual(rebalance_days(scarce, profiles, 3).get("moved"), [])
        # 全天型独占日不参与搬移；晚上型点不得被搬到非晚上时段
        prof2 = {**profiles, "夜A": {"best_time_slot": "晚上"}, "环球": {"duration_hours": 8}}
        night = {"days": [
            {"day": 1, "slots": [{"slot": "晚上", "spot": "夜A"}, {"slot": "上午", "spot": "甲"},
                                  {"slot": "下午", "spot": "乙"}]},
            {"day": 2, "slots": [{"slot": "上午", "spot": "丙"}]},
        ]}
        out2 = rebalance_days(night, prof2, 2)
        for d in out2["days"]:
            for s in d["slots"]:
                if s["spot"] == "夜A":
                    self.assertEqual(s["slot"], "晚上")               # 夜景型不会被搬到白天
        all_day = {"days": [
            {"day": 1, "slots": [{"slot": "全天", "spot": "环球"}]},
            {"day": 2, "slots": [{"slot": "上午", "spot": "甲"}]},
            {"day": 3, "slots": [{"slot": "上午", "spot": "乙"}, {"slot": "下午", "spot": "丙"}]},
        ]}
        out3 = rebalance_days(all_day, prof2, 3)
        self.assertEqual(out3["days"][0]["slots"][0]["slot"], "全天")   # 全天型点不被拆分/搬走
        self.assertEqual(out3["moved"], [])                          # 素材不够（3 天需 5 点）→ 不硬凑

    def test_pick_ticket_price_ignores_retail_items(self):
        """P6：园内小吃/商品/单独收费项目即使被标成 type=门票，也不当景区门票本体价。"""
        from pipeline.planner import pick_ticket_price

        self.assertIsNone(pick_ticket_price([{"item": "烤肠", "type": "门票", "amount": 12}]))
        self.assertIsNone(pick_ticket_price([{"item": "观光车票", "type": "门票", "amount": 20}]))
        self.assertEqual(pick_ticket_price([
            {"item": "观光车票", "type": "门票", "amount": 20},
            {"item": "大门票", "type": "门票", "amount": 60}]), 60.0)
        # 第三方代抢价仍照旧排除（原有口径不回退）
        self.assertIsNone(pick_ticket_price([{"item": "携程代抢票", "type": "门票", "amount": 899}]))



    def test_slot_price_labels_same_source(self):
        """行程槽位票价文案只读 catalog 同源价（ticket_price）；无价或旧 cost 字段一律不出价格行。"""
        from pipeline.trip_render import slot_price_labels

        self.assertEqual(slot_price_labels({"ticket_price": 60}),
                         ("门票：60 元（同源详情）", "门票 60 元"))
        self.assertEqual(slot_price_labels({"ticket_price": 0}),
                         ("门票：免费（同源详情）", "门票 免费"))
        self.assertEqual(slot_price_labels({"ticket_price": None, "cost": 12}), ("", ""))
        self.assertEqual(slot_price_labels({"cost": 12}), ("", ""))   # 旧 cost 字段不再出口
        self.assertEqual(slot_price_labels({}), ("", ""))

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

    def setUp(self):
        from crawler import base
        self._base = base
        self._saved_enabled = base.SOURCE_DOUYIN_ENABLED
        base.SOURCE_DOUYIN_ENABLED = True   # 提速测试直接驱动 fetch_videos，需先过开源闸门

    def tearDown(self):
        self._base.SOURCE_DOUYIN_ENABLED = self._saved_enabled

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

    def test_detail_node(self):
        """详情包取节点：aweme_detail / item_list / 裸节点三种结构都认，脏数据给空。"""
        from crawler.douyin import author_uid_of, detail_node

        self.assertEqual(detail_node({"aweme_detail": {"desc": "d"}}), {"desc": "d"})
        self.assertEqual(detail_node({"item_list": [{"desc": "d2"}]}), {"desc": "d2"})
        self.assertEqual(detail_node({"desc": "d3"}), {"desc": "d3"})
        self.assertEqual(detail_node({}), {})
        self.assertEqual(detail_node("不是字典"), {})
        self.assertIsNone(detail_node({"item_list": []}).get("desc"))
        # 作者 UID：整包与已取出的节点都认
        self.assertEqual(author_uid_of({"aweme_detail": {"author": {"uid": "u1"}}}), "u1")
        self.assertEqual(author_uid_of({"author": {"sec_uid": "s1"}}), "s1")
        self.assertIsNone(author_uid_of({"aweme_detail": {}}))

    def test_drain_detail_buffers_other_packets(self):
        """详情环节：找到详情包即返回，期间到达的评论包进缓冲不丢。"""
        from types import SimpleNamespace

        from crawler.douyin import (AWEME_DETAIL_TARGET, COMMENT_API_TARGET,
                                    DouyinCrawler, detail_node)

        def mk(url_part, body):
            return SimpleNamespace(url=f"https://www.douyin.com/aweme/v1/web/{url_part}/?a=1",
                                   response=SimpleNamespace(body=body))

        detail = mk(AWEME_DETAIL_TARGET, {"aweme_detail": {
            "desc": "文案", "author": {"uid": "u9"},
            "statistics": {"digg_count": 1200}, "create_time": 1767225600}})
        comment = mk(COMMENT_API_TARGET, {"comments": [{"text": "好看", "digg_count": 3}],
                                          "has_more": 0})

        class Listen:
            def __init__(self, packets): self.packets = packets
            def start(self, targets): pass
            def stop(self): pass
            def wait(self, count=1, timeout=None, fit_count=True, raise_err=None):
                return self.packets.pop(0) if self.packets else None

        class Page:
            def __init__(self, packets): self.listen = Listen(packets)
            def ele(self, sel, timeout=None): return None

        # 评论包先到、详情包后到：评论包必须留在缓冲里被后续环节消费
        c = DouyinCrawler(Page([comment, detail]))
        node = detail_node(c._drain_detail(timeout=1))
        self.assertEqual(node.get("desc"), "文案")
        self.assertEqual(node["statistics"]["digg_count"], 1200)
        self.assertEqual(len(c._pkt_buf), 1)                        # 评论包被暂存
        rows = c._comments_by_listen(max_n=10, container=None)
        self.assertEqual([r["text"] for r in rows], ["好看"])        # 缓冲被消费，没丢包
        self.assertEqual(c._pkt_buf, [])
        # 根本没详情包（接口改版）：不卡死，超时给空 dict 让 DOM 降级接手
        c2 = DouyinCrawler(Page([]))
        self.assertEqual(c2._drain_detail(timeout=0.5), {})

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
        """MCP 服务器注册了全套 9 个工具；plan_trip 参数面与 /api/trip 对齐（防再次漂移）。"""
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
        # 预算已退役不得回潮；start_date 与 api_server.TripIn 对齐（闭馆日校验）
        trip_tool = next(t for t in tools if t.name == "plan_trip")
        schema = getattr(trip_tool, "inputSchema", None) or getattr(trip_tool, "input_schema", None) or {}
        props = set((schema or {}).get("properties") or {})
        self.assertNotIn("budget", props)
        self.assertIn("start_date", props)
        self.assertEqual(
            props,
            {"city", "days", "hotel", "spots", "preferences", "start_date", "preference_mode"},
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


class TestCostGuard(unittest.TestCase):
    """成本护栏：用户硬要求是"只花免费额度与代金券，绝不扣现金余额"。

    对应三道防线：①联网搜索默认关闭（搜索插件属独立计费，抵扣口径不一致）；
    ②免费额度耗尽的 403 不重试并给可操作提示；③token 用量可核对。"""

    def _fake(self, completions):
        from types import SimpleNamespace

        return SimpleNamespace(api_key="x", base_url="y",
                               chat=SimpleNamespace(completions=completions))

    def test_web_search_off_by_default(self):
        """联网搜索默认关：不发 enable_search 就不可能产生独立计费的搜索费。"""
        import config
        from core.llm import _extra_body

        self.assertFalse(config.LLM_WEB_SEARCH)
        self.assertNotIn("enable_search", _extra_body(False, config.LLM_WEB_SEARCH))
        self.assertNotIn("search", _extra_body(False, config.LLM_WEB_SEARCH))

    def test_free_tier_exhausted_no_retry_and_hint(self):
        """403 AllocationQuota.FreeTierOnly：一次都不重试，提示里给换模型的出路。

        开了「免费额度用完即停」后额度耗尽就是这个错——它是防扣现金的保护，
        重试只会白等三轮并掩盖真正原因。"""
        import core.llm as llm

        class FreeTierErr(Exception):
            pass

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kw):
                self.calls += 1
                raise FreeTierErr("Error code: 403 - AllocationQuota.FreeTierOnly: "
                                  "free tier quota exhausted")

        fake = self._fake(FakeCompletions())
        orig = llm._client_and_model
        llm._client_and_model = lambda: (fake, "qwen-plus")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_json("s", "u", retries=3)
            self.assertEqual(fake.chat.completions.calls, 1)     # 一次都没重试
            msg = str(ctx.exception)
            self.assertIn("免费额度已用尽", msg)
            self.assertIn("用完即停", msg)
            self.assertIn("run_cli.py setup", msg)                # 给出可操作出路
            self.assertNotIn("充值", msg)                          # 不能误报成欠费
            with self.assertRaises(RuntimeError) as ctx2:
                llm.chat_text("s", "u")
            self.assertIn("免费额度已用尽", str(ctx2.exception))
        finally:
            llm._client_and_model = orig

    def test_free_tier_distinct_from_arrearage(self):
        """额度耗尽与真欠费走不同提示：前者换模型即可，后者才需要充值。"""
        from core.llm import _is_billing, _is_free_tier_exhausted

        free = Exception("403 AllocationQuota.FreeTierOnly")
        arrears = Exception("Access denied, please make sure your account is in good standing")
        self.assertTrue(_is_free_tier_exhausted(free))
        self.assertFalse(_is_billing(free))
        self.assertTrue(_is_billing(Exception("Arrearage: account overdue")))
        self.assertFalse(_is_free_tier_exhausted(arrears))

    def test_usage_ledger_accumulates(self):
        """token 用量账本：累计调用次数与输入/输出 token，按模型分组可核对。"""
        from types import SimpleNamespace

        import core.llm as llm

        class FakeCompletions:
            def create(self, **kw):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))],
                    usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=300))

        fake = self._fake(FakeCompletions())
        orig = llm._client_and_model
        llm._client_and_model = lambda: (fake, "qwen-plus")
        llm.reset_usage()
        try:
            llm.chat_json("s", "u", retries=1)
            llm.chat_json("s", "u", retries=1)
            llm.chat_text("s", "u")
            s = llm.usage_summary()
            self.assertEqual(s["calls"], 3)
            self.assertEqual(s["prompt_tokens"], 3600)
            self.assertEqual(s["completion_tokens"], 900)
            self.assertEqual(s["total_tokens"], 4500)
            self.assertEqual(s["by_model"]["qwen-plus"], {"calls": 3, "tokens": 4500})
            note = llm.usage_note()
            self.assertIn("3 次调用", note)
            self.assertIn("4500 token", note)
            self.assertIn("qwen-plus", note)
        finally:
            llm._client_and_model = orig
            llm.reset_usage()

    def test_usage_without_usage_field(self):
        """服务商不返 usage 时只计次数，不猜 token（不编造消耗数据）。"""
        from types import SimpleNamespace

        import core.llm as llm

        class FakeCompletions:
            def create(self, **kw):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))],
                    usage=None)

        fake = self._fake(FakeCompletions())
        orig = llm._client_and_model
        llm._client_and_model = lambda: (fake, "m")
        llm.reset_usage()
        try:
            llm.chat_json("s", "u", retries=1)
            s = llm.usage_summary()
            self.assertEqual(s["calls"], 1)
            self.assertEqual(s["total_tokens"], 0)      # 拿不到就是 0，不估
        finally:
            llm._client_and_model = orig
            llm.reset_usage()
        self.assertEqual(llm.usage_note(), "LLM 用量：本次未调用")

    def test_amap_cap_within_free_tier(self):
        """高德日上限护栏不超个人开发者免费配额（超额才会转付费）。"""
        import config

        self.assertGreater(config.AMAP_DAILY_CAP, 0)
        self.assertLessEqual(config.AMAP_DAILY_CAP, 5000)


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
        """MD 渲染（新 TripPlan 渲染器）：注意事项分点，避坑/费用加粗；字符串 notes 自动归一化。"""
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.trip_render import render_markdown

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "", "transport": "", "cost": 0,
             "reasons": "", "notes": "别穿高跟鞋；门票120元；早去人少", "food": ""}]}]}
        profiles = {"云冈石窟": {
            "duration_hours": None, "best_time_slot": "全天", "highlights": [],
            "avoid": [], "food": [], "photo_spots": [], "tips": [], "cost_items": []}}
        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan)
        tp = build_trip_plan(meta={"city": "大同", "days": 1}, decisions=decs, plan=plan).to_dict()
        md = render_markdown(tp)
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
        """无高德 Key 时地理全链路返回 None（降级不阻断）。

        显式清空 Key 而不只靠模块级防护：本用例的语义就是"无 Key"，
        若靠 .env 恰好没配来通过，用户一配 Key 就会真发 HTTP 并失败。"""
        from core import geo

        orig = geo.AMAP_API_KEY
        geo.AMAP_API_KEY = ""
        try:
            self.assertFalse(geo.available())
            self.assertIsNone(geo.route_advice("113,40", "114,41", "大同"))
            self.assertIsNone(geo.geocode_poi("甲", "大同"))
            self.assertIsNone(geo.travel_time("113,40", "114,41", "大同"))
            self.assertIsNone(geo.poi_detail("甲", "大同"))
        finally:
            geo.AMAP_API_KEY = orig

    def test_transit_legs_degrades_without_stops(self):
        """换乘描述：站数/站名缺失时只少显示缺的那段，绝不留半截括号。

        实测触发：高德地铁线路的 via_num_stops 可能为空，旧写法输出
        "地铁2号线(春熙路→通惠门, 站)"——读起来像缺字。"""
        from core.geo import _transit_legs

        def seg(name, stops, dep, arr):
            return {"bus": {"buslines": [{"name": name, "via_num_stops": stops,
                                          "departure_stop": {"name": dep},
                                          "arrival_stop": {"name": arr}}]}}

        full = {"segments": [seg("地铁2号线(犀浦→龙泉驿)", "5", "春熙路", "通惠门")]}
        self.assertEqual(_transit_legs(full), "地铁2号线(春熙路→通惠门, 5站)")

        no_stops = {"segments": [seg("地铁2号线", "", "春熙路", "通惠门")]}
        self.assertEqual(_transit_legs(no_stops), "地铁2号线(春熙路→通惠门)")
        self.assertNotIn(", 站", _transit_legs(no_stops))

        no_names = {"segments": [seg("603路", "12", "", "")]}
        self.assertEqual(_transit_legs(no_names), "603路")

        two = {"segments": [seg("地铁2号线", "3", "甲", "乙"), seg("603路", "", "乙", "丙")]}
        self.assertEqual(_transit_legs(two), "地铁2号线(甲→乙, 3站) 换乘 603路(乙→丙)")

        self.assertEqual(_transit_legs({"segments": []}), "")
        self.assertEqual(_transit_legs({}), "")


class TestFoodsRender(unittest.TestCase):
    def test_render_foods_and_echo(self):
        """餐厅详情卡/美食榜/概览餐厅数/参数回显全链路（新 TripPlan 渲染器）。"""
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.trip_render import render_markdown

        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "", "transport": "",
             "reasons": "", "notes": "", "food": "凤临阁：烧麦（人均80元）"}]}]}
        profiles = {"云冈石窟": {
            "duration_hours": None, "best_time_slot": "全天", "highlights": [],
            "avoid": [], "food": [], "photo_spots": [], "tips": [],
            "cost_items": [{"item": "门票", "type": "门票", "amount": 120.0}]}}
        foods = {"凤临阁": {
            "duration_hours": None, "best_time_slot": "全天", "highlights": ["百花烧麦"],
            "avoid": ["饭点排队久"], "food": [], "photo_spots": [], "tips": [],
            "cost_items": [{"item": "人均", "type": "餐饮人均", "amount": 80.0}]}}
        decs = build_decisions(profiles=profiles, food_profiles=foods, heat_rows=[],
                               official_facts={}, plan=plan)
        snap = {"user_spots": ["云冈石窟"], "foods": foods,
                "overview": {"days": 1, "spots": 1, "slots": 1, "foods": 1}}
        tp = build_trip_plan(meta={"city": "大同", "days": 1, "stay": "古城内",
                                  "prefs": "喜欢历史", "preference_mode": "均衡"},
                             decisions=decs, plan=plan, snap=snap).to_dict()
        md = render_markdown(tp)
        self.assertIn("凤临阁：烧麦（人均80元）", md)          # 行程槽位美食引导
        self.assertIn("## 美食榜（未排入行程时间线", md)
        self.assertIn("百花烧麦", md)                          # 招牌进详情卡
        self.assertIn("美食候选 **1 家**", md)                 # 概览卡
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


class TestSpotDecision(unittest.TestCase):
    """M1-1 统一决策对象（pipeline.decision）：证据映射、组装、状态标注、排序。"""

    def test_evidence_mapping(self):
        from pipeline.decision import evidence_from_conf, spot_evidence
        self.assertEqual(evidence_from_conf("高置信度", 3), "强")
        self.assertEqual(evidence_from_conf("高置信度", 2), "中")   # 独立来源不足 3 降为中
        self.assertEqual(evidence_from_conf("中置信度", 9), "中")
        self.assertEqual(evidence_from_conf("低置信度", 1), "弱")
        self.assertEqual(evidence_from_conf(None, None), "弱")      # 信息缺失保守给弱
        self.assertEqual(spot_evidence("强", []), "强")             # 验证结论优先
        self.assertEqual(spot_evidence(None, [{"conf_level": "高置信度"}, {"conf_level": "高置信度"}]), "强")
        self.assertEqual(spot_evidence(None, [{"conf_level": "高置信度"}]), "中")
        self.assertEqual(spot_evidence(None, [{"conf_level": "中置信度"}, {"conf_level": "中置信度"}]), "中")
        self.assertEqual(spot_evidence(None, []), "弱")

    def test_normalize_official_and_expiry(self):
        from pipeline.decision import normalize_official, official_expired
        f = normalize_official({"price": "120", "open_hours": "8:00-17:00", "close_day": "周一"})
        self.assertEqual(f.price, 120.0)
        self.assertEqual(f.close_day, "周一")
        self.assertFalse(f.missing)
        self.assertIsNone(normalize_official({"price": "免费"}).price)   # 无法解析保持 None，不退化成 0
        self.assertTrue(normalize_official(None).missing)
        self.assertTrue(official_expired(normalize_official({"price": 1, "valid_until": "2026-01-01"}), "2026-09-04"))
        self.assertFalse(official_expired(normalize_official({"price": 1, "valid_until": "2026-12-01"}), "2026-09-04"))
        self.assertFalse(official_expired(normalize_official({"price": 1}), "2026-09-04"))  # 无有效期不判过期

    def test_build_decisions_union_states_and_order(self):
        """F1.1：每个被圈定候选（含淘汰）都查得到结论；状态标注与排序正确。"""
        from pipeline.decision import build_decisions
        candidates = [
            {"name": "环球影城", "category": "景点", "reason": "主题乐园"},
            {"name": "冷门点", "category": "景点", "reason": "凑数"},
        ]
        verify = [
            {"name": "环球影城", "verdict": "keep", "evidence": "强", "pitfall_risk": "低", "reason": "口碑好"},
            {"name": "冷门点", "verdict": "drop", "evidence": "弱", "pitfall_risk": "高", "reason": "证据不足"},
        ]
        profiles = {"环球影城": {"best_time_slot": "全天"}, "什刹海": {"best_time_slot": "晚上"}}
        heat = [{"spot": "环球影城", "score": 0.9, "videos": 5, "likes": 100,
                 "comments": 50, "mkt_ratio": 0.1, "trend": "近期热度上升"}]
        plan = {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "环球影城", "reasons": "必去"}]}],
                "summary_note": "什刹海列为备选，距离远"}
        decs = build_decisions(candidates=candidates, verify_results=verify, profiles=profiles,
                               heat_rows=heat, sources_by_spot={"环球影城": ["u1"]}, plan=plan)
        by = {d.name: d for d in decs}
        self.assertEqual(by["环球影城"].decision.state, "入选")
        self.assertEqual(by["环球影城"].decision.day, 1)
        self.assertEqual(by["环球影城"].decision.slot, "上午")
        self.assertEqual(by["环球影城"].heat.score, 0.9)
        self.assertEqual(by["环球影城"].verify.evidence, "强")
        self.assertEqual(by["什刹海"].decision.state, "备选")      # summary_note 点名
        self.assertEqual(by["冷门点"].decision.state, "淘汰")      # verdict=drop 未排入
        self.assertEqual(by["冷门点"].verify.reason, "证据不足")   # 淘汰理由必须保留
        self.assertEqual(decs[0].name, "环球影城")                # 入选排最前
        row = by["环球影城"].to_row()
        self.assertEqual(row["state"], "入选")
        self.assertEqual(row["evidence"], "强")

    def test_unresearched_and_hard_reason(self):
        from pipeline.decision import build_decisions, DecisionInfo
        decs = build_decisions(candidates=[{"name": "被截断点", "category": "景点"}], plan={"days": []})
        d = {x.name: x for x in decs}["被截断点"]
        self.assertFalse(d.researched)                 # 无要点无来源
        self.assertEqual(d.decision.state, "")         # 既不入选也不冒充淘汰
        self.assertIn("未进入调研", d.decision.reason)
        self.assertTrue(DecisionInfo(reason="闭馆日冲突，无法安排").has_hard_reason)
        self.assertFalse(DecisionInfo(reason="就是不太想去").has_hard_reason)

    def test_food_recommendation_marks_in(self):
        """food 字段点名推荐的餐厅标入选（不误显示备选、不触发 R2）。"""
        from pipeline.decision import build_decisions
        plan = {"days": [{"day": 1, "slots": [
            {"slot": "晚上", "spot": "什刹海", "reasons": "", "food": "四季民福：烤鸭（人均10）"}]}],
            "summary_note": ""}
        decs = build_decisions(profiles={"什刹海": {"best_time_slot": "全天"}},
                               food_profiles={"四季民福": {"best_time_slot": "全天"}}, plan=plan)
        by = {d.name: d for d in decs}
        self.assertEqual(by["什刹海"].decision.state, "入选")
        self.assertEqual(by["四季民福"].decision.state, "入选")     # 被推荐为餐食 → 入选
        self.assertTrue(by["四季民福"].is_food)
        self.assertIn("餐食推荐", by["四季民福"].decision.reason)


class TestQualityGate(unittest.TestCase):
    """M1-3 质量门禁（pipeline.qc）：逐条正反夹具 + 报告聚合（R6 预算随预算模块退役）。"""

    def _dec(self, name, *, state="", evidence="弱", heat=0.0, videos=0,
             sources=None, reason="", close_day="", official_price=None,
             day=None, slot=""):
        from pipeline.decision import SpotDecision, VerifyInfo, HeatInfo, DecisionInfo, OfficialFact
        d = SpotDecision(name=name)
        verdict = "keep" if state == "入选" else ("drop" if state == "淘汰" else "")
        d.verify = VerifyInfo(evidence=evidence, verdict=verdict)
        d.heat = HeatInfo(score=heat, videos=videos)
        d.official = OfficialFact(close_day=close_day, price=official_price)
        d.sources = list(sources or [])
        d.decision = DecisionInfo(state=state, day=day, slot=slot, reason=reason)
        return d

    def _gate(self, **kw):
        from pipeline.qc import run_quality_gate
        kw.setdefault("decisions", [])
        kw.setdefault("plan", {"days": []})
        kw.setdefault("profiles", {})
        kw.setdefault("days", 1)
        return run_quality_gate(**kw)

    def test_r1_coverage(self):
        prof = {f"P{i}": {} for i in range(4)}
        plan_few = {"days": [{"day": 1, "slots": [{"spot": "P0", "slot": "上午"}, {"spot": "P1", "slot": "下午"}]},
                             {"day": 2, "slots": []}]}
        self.assertEqual(self._gate(plan=plan_few, profiles=prof, days=2).get("R1").status, "fail")
        # 天数必须齐：要 2 天就得有 2 天安排（旧用例只给 1 天却期望 pass，
        # 天数覆盖判定上线后不再成立：那正是"要 3 天只排 2 天"的缩小版）
        plan_ok = {"days": [{"day": 1, "slots": [{"spot": "P0", "slot": "上午"}, {"spot": "P1", "slot": "下午"}]},
                            {"day": 2, "slots": [{"spot": "P2", "slot": "上午"}, {"spot": "P3", "slot": "下午"}]}]}
        self.assertEqual(self._gate(plan=plan_ok, profiles=prof, days=2).get("R1").status, "pass")
        plan_many = {"days": [{"day": 1, "slots": [{"spot": f"P{i}", "slot": "上午"} for i in range(6)]}]}
        self.assertEqual(self._gate(plan=plan_many, profiles={f"P{i}": {} for i in range(6)}, days=1).get("R1").status, "warn")

    def test_r2_no_silent_drop(self):
        self.assertEqual(self._gate(decisions=[self._dec("环球", state="备选", heat=0.95, videos=5, reason="")]).get("R2").status, "fail")
        self.assertEqual(self._gate(decisions=[self._dec("环球", state="备选", heat=0.95, videos=5, reason="单日时长不足")]).get("R2").status, "pass")
        self.assertEqual(self._gate(decisions=[self._dec("甲", state="淘汰", evidence="强", reason="证据不足")]).get("R2").status, "fail")
        self.assertEqual(self._gate(decisions=[self._dec("环球", state="入选", heat=0.95, videos=5)]).get("R2").status, "pass")

    def test_r2_skips_food(self):
        """R2 只判行程骨架；强证据美食未入选交 R7，不误报 R2。"""
        food = self._dec("某餐厅", state="备选", evidence="强", heat=0.9, videos=5)
        food.category = "美食"
        self.assertEqual(self._gate(decisions=[food]).get("R2").status, "pass")

    def test_r3_time_slot_and_close_day(self):
        prof = {"夜游A": {"best_time_slot": "晚上"}}
        plan_bad = {"days": [{"day": 1, "slots": [{"spot": "夜游A", "slot": "下午"}]}]}
        self.assertEqual(self._gate(plan=plan_bad, profiles=prof).get("R3").status, "fail")
        plan_ok = {"days": [{"day": 1, "slots": [{"spot": "夜游A", "slot": "晚上"}]}]}
        self.assertEqual(self._gate(plan=plan_ok, profiles=prof).get("R3").status, "pass")
        decs = [self._dec("博物馆", state="入选", close_day="周一", day=1, slot="上午")]
        plan_mu = {"days": [{"day": 1, "slots": [{"spot": "博物馆", "slot": "上午"}]}]}
        self.assertEqual(self._gate(plan=plan_mu, decisions=decs, day_weekdays=["周一", "周二"]).get("R3").status, "fail")
        self.assertEqual(self._gate(plan=plan_mu, decisions=decs, day_weekdays=["周二", "周三"]).get("R3").status, "pass")

    def test_r4_time_feasible(self):
        prof = {"A": {"duration_hours": 5}, "B": {"duration_hours": 5}, "C": {"duration_hours": 3}}
        plan = {"days": [{"day": 1, "slots": [{"spot": "A", "slot": "上午"}, {"spot": "B", "slot": "下午"}, {"spot": "C", "slot": "晚上"}]}]}
        self.assertEqual(self._gate(plan=plan, profiles=prof).get("R4").status, "fail")   # 13h > 11h
        prof2 = {"A": {"duration_hours": 3}, "B": {"duration_hours": 3}}
        plan2 = {"days": [{"day": 1, "slots": [{"spot": "A", "slot": "上午"}, {"spot": "B", "slot": "下午"}]}]}
        self.assertEqual(self._gate(plan=plan2, profiles=prof2).get("R4").status, "pass")
        plan3 = {"days": [{"day": 1, "slots": [{"spot": "X", "slot": "上午"}]}]}
        self.assertEqual(self._gate(plan=plan3, profiles={"X": {"duration_hours": None}}).get("R4").status, "pass")

    def test_r5_skip_in_m1(self):
        self.assertEqual(self._gate().get("R5").status, "skip")

    def test_r7_food(self):
        plan = {"days": [{"day": 1, "slots": [{"spot": "A", "slot": "下午", "food": "全聚德：烤鸭"}]}]}
        self.assertEqual(self._gate(plan=plan, food_profiles={"凤临阁": {}}).get("R7").status, "fail")   # 编造店名
        self.assertEqual(self._gate(plan=plan, food_profiles=None).get("R7").status, "skip")            # 无候选无兜底
        plan2 = {"days": [{"day": 1, "slots": [{"spot": "A", "slot": "下午", "food": ""}]}]}
        self.assertEqual(self._gate(plan=plan2, food_profiles={"凤临阁": {}}).get("R7").status, "pass")   # 餐饮解耦：漏排餐厅属预期
        plan3 = {"days": [{"day": 1, "slots": [{"spot": "A", "slot": "下午", "food": "凤临阁：烧麦"}]}]}
        self.assertEqual(self._gate(plan=plan3, food_profiles={"凤临阁": {}}).get("R7").status, "pass")

    def test_r8_pitfall_attribution(self):
        pit = [{"source": "u1", "claim": "人太多"}, {"source": "u2", "claim": "票难买"}]
        decs_ok = [self._dec("A", state="入选", sources=["u1"]), self._dec("B", state="备选", sources=["u2"])]
        self.assertEqual(self._gate(pitfall=pit, decisions=decs_ok).get("R8").status, "pass")
        decs_bad = [self._dec("A", state="入选", sources=["u1"]), self._dec("B", state="淘汰", sources=["u2"])]
        self.assertEqual(self._gate(pitfall=pit, decisions=decs_bad).get("R8").status, "fail")
        self.assertEqual(self._gate(pitfall=[], decisions=decs_ok).get("R8").status, "skip")

    def test_r9_timeliness(self):
        from pipeline.decision import OfficialFact
        self.assertEqual(self._gate().get("R9").status, "skip")                       # 无官方数据
        decs = [self._dec("甲", official_price=100)]
        self.assertEqual(self._gate(decisions=decs, today="2026-09-04").get("R9").status, "pass")
        decs[0].official = OfficialFact(price=100, valid_until="2026-01-01")
        self.assertEqual(self._gate(decisions=decs, today="2026-09-04").get("R9").status, "warn")

    def test_r10_sources(self):
        self.assertEqual(self._gate(decisions=[self._dec("A", state="入选", sources=[])]).get("R10").status, "fail")
        self.assertEqual(self._gate(decisions=[self._dec("A", state="入选", sources=["u1"])]).get("R10").status, "pass")

    def test_report_aggregation_and_finalize(self):
        from pipeline.qc import run_quality_gate, finalize
        decs = [self._dec("环球", state="备选", heat=0.95, videos=5, reason="", sources=["u1"])]
        plan = {"days": [{"day": 1, "slots": [{"spot": "环球", "slot": "下午"}]}]}
        rep = run_quality_gate(decisions=decs, plan=plan, profiles={"环球": {"best_time_slot": "全天"}},
                               days=2)
        self.assertFalse(rep.passed)                                    # R2 静默丢弃
        self.assertGreater(len(rep.issues), 0)
        self.assertLess(rep.score, 100)
        self.assertIn("R2", [c.rule_id for c in rep.fails])
        finalize(rep, repair_rounds=2)
        self.assertEqual(rep.repair_rounds, 2)
        self.assertTrue(any("R2" in u for u in rep.unresolved))         # fail 项进已知妥协
        self.assertGreaterEqual(rep.problem_count(), 2 * len(rep.fails))
        d = rep.to_dict()
        self.assertEqual(len(d["checks"]), 11)                          # R1~R12（R6 退役）全部登记
        self.assertIn("score", d)


class TestGateWiringAndRender(unittest.TestCase):
    """M1-4/M1-8：门禁接线（plan_itinerary 回炉）+ 选点决策表/质量分卡渲染。"""

    def _fixture(self):
        from pipeline.decision import build_decisions
        from pipeline.qc import run_quality_gate, finalize
        profiles = {
            "环球影城": {"duration_hours": 6, "best_time_slot": "全天", "highlights": ["必玩"],
                     "avoid": [], "food": [], "photo_spots": [], "tips": [],
                     "cost_items": [{"item": "门票", "type": "门票", "amount": 650.0}]},
            "什刹海": {"duration_hours": 2, "best_time_slot": "晚上", "highlights": [],
                    "avoid": [], "food": [], "photo_spots": [], "tips": [], "cost_items": []},
        }
        plan = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "环球影城", "duration": "约6小时", "transport": "",
             "reasons": "必玩", "notes": [], "food": ""}]}],
            "summary_note": "什刹海列为备选"}
        decs = build_decisions(
            candidates=[{"name": "环球影城", "category": "景点", "reason": "主题乐园"},
                        {"name": "冷门点", "category": "景点", "reason": "凑数"}],
            verify_results=[{"name": "冷门点", "verdict": "drop", "evidence": "弱",
                             "pitfall_risk": "高", "reason": "证据不足"}],
            profiles=profiles,
            heat_rows=[{"spot": "环球影城", "score": 0.9, "videos": 5, "likes": 999,
                        "comments": 50, "mkt_ratio": 0.1, "trend": "近期热度上升"}],
            sources_by_spot={"环球影城": ["https://x/1"]}, plan=plan)
        rep = run_quality_gate(decisions=decs, plan=plan, profiles=profiles, days=2)
        finalize(rep, repair_rounds=1)
        return plan, profiles, decs, rep

    def test_decision_rows_and_quality_dict(self):
        """决策表模板投影（新渲染器）+ 质量报告序列化（quality 归一已由 to_dict 承担）。"""
        from pipeline.trip_render import _decision_rows_for_template
        _, _, decs, rep = self._fixture()
        rows = _decision_rows_for_template([d.to_row() for d in decs])
        gl = next(r for r in rows if r["name"] == "环球影城")
        self.assertEqual(gl["state"], "入选")
        self.assertEqual(gl["heat"], "0.90")
        self.assertEqual(gl["mkt"], "10%")
        self.assertEqual(gl["state_icon"], "✅")
        self.assertTrue(any(r["name"] == "冷门点" and r["state"] == "淘汰" for r in rows))  # 淘汰点也在表内
        q = rep.to_dict()
        self.assertEqual(len(q["checks"]), 11)
        self.assertIn("score", q)

    def test_render_md_sections(self):
        from pipeline.decision import build_trip_plan
        from pipeline.trip_render import render_markdown
        plan, profiles, decs, rep = self._fixture()
        tp = build_trip_plan(meta={"city": "北京", "days": 2, "stay": "三环"},
                             decisions=decs, plan=plan, quality=rep).to_dict()
        md = render_markdown(tp)
        self.assertIn("## 选点决策表", md)
        self.assertIn("## 质量分卡", md)
        self.assertIn("已知妥协", md)          # R1/R2 fail → unresolved
        self.assertIn("冷门点", md)             # 淘汰点可见（F7.1）
        self.assertIn("证据不足", md)           # 淘汰理由来自 verify.reason

    def test_render_html_sections(self):
        from pipeline.decision import build_trip_plan
        from pipeline.trip_render import render_html
        plan, profiles, decs, rep = self._fixture()
        tp = build_trip_plan(meta={"city": "北京", "days": 2, "stay": "三环"},
                             decisions=decs, plan=plan, quality=rep).to_dict()
        html = render_html(tp)
        self.assertIn("选点决策表", html)
        self.assertIn("质量分卡", html)
        self.assertIn("decision-table", html)
        self.assertIn("冷门点", html)

    def test_backward_compat_no_sections(self):
        """无候选/无质量报告时不渲染决策表与质量分卡（空数据降级不空窗）。"""
        from pipeline.decision import build_trip_plan
        from pipeline.trip_render import render_markdown
        plan = {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "甲", "duration": "", "transport": "",
                                              "reasons": "", "notes": [], "food": ""}]}], "summary_note": ""}
        tp = build_trip_plan(meta={"city": "城", "days": 1}, decisions=[], plan=plan).to_dict()
        md = render_markdown(tp)
        self.assertNotIn("## 选点决策表", md)
        self.assertNotIn("## 质量分卡", md)

    def test_plan_itinerary_extra_issues_repair(self):
        """extra_issues 触发回炉；覆盖率不变差即采纳重试版（无 extra 且达标时不回炉）。"""
        import pipeline.planner as pp
        profiles = {"甲": {"duration_hours": None, "best_time_slot": "全天", "highlights": [],
                       "avoid": [], "food": [], "photo_spots": [], "tips": [], "cost_items": []}}
        calls = {"n": 0}

        def fake(system, user, **k):
            calls["n"] += 1
            if "本版必须修正" in user:      # 回炉调用
                return {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "甲"}]}], "summary_note": "已按门禁修正"}
            return {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "甲"}]}], "summary_note": "初版"}

        orig = pp.chat_json
        try:
            pp.chat_json = fake
            plan = pp.plan_itinerary("城", 1, "", profiles, [], "", extra_issues=["把说明写清"])
            self.assertEqual(calls["n"], 2)                        # 初次 + 回炉
            self.assertEqual(plan["summary_note"], "已按门禁修正")   # 覆盖率持平也采纳（extra 分支）
            calls["n"] = 0
            plan2 = pp.plan_itinerary("城", 1, "", profiles, [], "")   # 无 extra 且达标
            self.assertEqual(calls["n"], 1)                        # 不触发回炉，省一次 LLM 调用
            self.assertEqual(plan2["summary_note"], "初版")
        finally:
            pp.chat_json = orig

class TestSourceGate(unittest.TestCase):
    """开源合规闸门：UGC 源默认关闭、护栏校验、验证码检测停采（只停不绕）。"""

    def setUp(self):
        from crawler import base
        self.base = base
        self._saved = (base.SOURCE_DOUYIN_ENABLED, base.REQUEST_DELAY_MIN,
                       base.REQUEST_DELAY_MAX, base._noticed, base._session_stopped)

    def tearDown(self):
        b = self.base
        (b.SOURCE_DOUYIN_ENABLED, b.REQUEST_DELAY_MIN, b.REQUEST_DELAY_MAX,
         b._noticed, b._session_stopped) = self._saved

    def test_default_off_raises(self):
        from crawler.base import SourceDisabled
        self.base.SOURCE_DOUYIN_ENABLED = False
        with self.assertRaises(SourceDisabled):
            self.base.require_ugc_source()
        self.assertFalse(self.base.douyin_enabled())

    def test_enabled_passes_and_notices_once(self):
        self.base.SOURCE_DOUYIN_ENABLED = True
        logs = []
        self.base.require_ugc_source(log=logs.append)
        self.assertTrue(self.base.douyin_enabled())
        self.assertEqual(len(logs), 1)                    # 免责告知打印一次
        self.base.require_ugc_source(log=logs.append)
        self.assertEqual(len(logs), 1)                    # 不重复打印

    def test_guardrail_zero_delay_rejects(self):
        self.base.SOURCE_DOUYIN_ENABLED = True
        self.base.REQUEST_DELAY_MIN = 0.0
        self.base.REQUEST_DELAY_MAX = 0.0
        with self.assertRaises(RuntimeError):             # 频控为 0 拒绝启用
            self.base.require_ugc_source()

    def test_captcha_detected_and_session_stop(self):
        class CaptchaPage:
            title = "验证码中间页"
            html = "<script>window.TTGCaptcha</script>"

        class NormalPage:
            title = "抖音"
            html = "<html></html>"

        self.assertTrue(self.base.captcha_detected(CaptchaPage()))
        self.assertFalse(self.base.captcha_detected(NormalPage()))
        self.base.stop_session()
        self.assertTrue(self.base.session_stopped())
        self.base.reset_session()
        self.assertFalse(self.base.session_stopped())


class TestAmapQuota(unittest.TestCase):
    """高德用量护栏：按日计数 + 达上限自动降级（不烧配额、不发请求）。"""

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        from core import geo
        self.geo = geo
        self._saved = (geo.AMAP_API_KEY, geo.AMAP_DAILY_CAP, geo._USAGE_FILE)
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._tmp = Path(path)
        geo._USAGE_FILE = Path(path)
        geo._cap_warned = False

    def tearDown(self):
        import os
        g = self.geo
        (g.AMAP_API_KEY, g.AMAP_DAILY_CAP, g._USAGE_FILE) = self._saved
        try:
            os.remove(self._tmp)   # 只删临时计数文件，不碰真实 data/amap_usage.json
        except OSError:
            pass

    def test_cap_zero_blocks_without_http(self):
        g = self.geo
        g.AMAP_API_KEY = "fake"
        g.AMAP_DAILY_CAP = 0
        self.assertTrue(g._quota_exhausted())
        self.assertIsNone(g.geocode_poi("配额测试点", "北京"))   # 上限 0 → 不发请求直接降级
        self.assertIsNone(g.route_advice("116,39", "116,40"))

    def test_counter_increments_and_daily_reset(self):
        import json
        from datetime import date, timedelta
        g = self.geo
        g.AMAP_DAILY_CAP = 300
        g._bump_usage()
        g._bump_usage()
        self.assertEqual(g.amap_usage()["today"], 2)
        self.assertEqual(g.amap_usage()["cap"], 300)
        # 跨日重置：计数文件写成昨天 → 今日归 0
        g._USAGE_FILE.write_text(
            json.dumps({"date": (date.today() - timedelta(days=1)).isoformat(), "count": 5}),
            encoding="utf-8")
        self.assertEqual(g.amap_usage()["today"], 0)
        self.assertFalse(g._quota_exhausted())


class TestM1GoldenCases(unittest.TestCase):
    """M1 出口黄金用例（PRD §15 Case A 子集 + Case B）新增保证的离线断言。

    仅测 M1 新增部分（#3/#4/#6/#8/#9 已由既有 R3/R8/决策表/finalize/R4 测试覆盖）：
    #12 餐饮解耦（时间线不排餐厅）、#10/#11 R11/R12 接口位、
    Case B#2 主体归属校验（F-A3）。预算模块已退役，恒等/弹性用例随之删除。"""

    def _profiles(self):
        return {
            "云冈石窟": {"duration_hours": 2.5, "best_time_slot": "全天", "highlights": ["石窟"],
                        "avoid": [], "food": [], "photo_spots": [], "tips": [],
                        "cost_items": [{"item": "门票", "type": "门票", "amount": 120.0}]},
            "华严寺": {"duration_hours": 1.5, "best_time_slot": "全天", "highlights": [],
                      "avoid": [], "food": [], "photo_spots": [], "tips": [],
                      "cost_items": [{"item": "门票", "type": "门票", "amount": 50.0}]},
        }

    def _plan(self):
        return {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "云冈石窟", "duration": "", "transport": "", "cost": 120,
             "reasons": "", "notes": [], "food": "", "pitfall_quotes": []},
            {"slot": "下午", "spot": "华严寺", "duration": "", "transport": "", "cost": 50,
             "reasons": "", "notes": [], "food": "", "pitfall_quotes": []},
        ]}]}

    # #10/#11：R11/R12 接口位 SKIP（不计分）
    def test_r11_r12_skip_interface(self):
        from pipeline.qc import run_quality_gate

        rep = run_quality_gate(decisions=[], plan={"days": []}, profiles={}, days=1)
        self.assertEqual(len(rep.checks), 11)
        self.assertEqual(rep.get("R11").status, "skip")
        self.assertEqual(rep.get("R12").status, "skip")
        self.assertNotIn("R11", [c.rule_id for c in rep.fails])
        self.assertNotIn("R12", [c.rule_id for c in rep.fails])

    # Case B#2：F-A3 主体归属校验 + substring 容忍
    def test_attribution_check(self):
        from pipeline.decision import apply_attribution_check, find_misattributed

        keep, suspect = find_misattributed(
            ["灵隐寺的素面很惊艳", "西湖边雷峰塔值得看"], "西湖", ["灵隐寺"])
        self.assertEqual(suspect, ["灵隐寺的素面很惊艳"])
        self.assertIn("西湖边雷峰塔值得看", keep)
        # substring 容忍：“西湖醋鱼”含“西湖”，挂西湖档案不误删
        keep2, sus2 = find_misattributed(["推荐西湖醋鱼"], "西湖", ["西湖醋鱼"])
        self.assertEqual(sus2, [])
        # 档案级：apply_attribution_check 就地剔除并回报
        profs = {
            "西湖": {"highlights": ["断桥残雪"], "avoid": ["灵隐寺停车难"], "tips": []},
            "灵隐寺": {"highlights": ["素面"], "avoid": [], "tips": []},
        }
        removed = apply_attribution_check(profs)
        self.assertEqual(removed, {"西湖": ["灵隐寺停车难"]})
        self.assertEqual(profs["西湖"]["avoid"], [])

    # #12：plan_itinerary 解耦——即使 LLM 返回餐厅也清空 slot.food
    def test_food_decoupled_plan_clears_timeline(self):
        import pipeline.planner as pp

        orig = pp.chat_json

        def fake(system, user, **kw):
            return {"summary_note": "", "days": [{"day": 1, "slots": [
                {"slot": "下午", "spot": "A", "food": "全聚德：烤鸭（人均120元）"},
                {"slot": "晚上", "spot": "B", "food": "海底捞"},
            ]}]}
        pp.chat_json = fake
        try:
            profs = {
                "A": {"duration_hours": 2, "best_time_slot": "下午", "highlights": [], "avoid": [],
                      "food": [], "photo_spots": [], "tips": [], "cost_items": []},
                "B": {"duration_hours": 2, "best_time_slot": "晚上", "highlights": [], "avoid": [],
                      "food": [], "photo_spots": [], "tips": [], "cost_items": []},
            }
            plan = pp.plan_itinerary("北京", 1, "酒店", profs, [], "")
            for d in plan["days"]:
                for s in d["slots"]:
                    self.assertEqual(s.get("food", ""), "")
        finally:
            pp.chat_json = orig


class TestM2aGoldenCases(unittest.TestCase):
    """M2a 出口黄金用例（PRD §15 Case A #2/#5 + F-C1/F-C3）。

    官方事实三层降级（种子 YAML 可断言）、门票以官方为准并带来源、
    多源冲突仲裁取官方、过期事实仍挂决策对象供 R9 警示。
    （R3 闭馆 fail / R9 过期的纯函数已由 TestQualityGate 覆盖。）"""

    def _profiles(self, ticket=None):
        p = {"甲": {"duration_hours": 2, "best_time_slot": "下午", "highlights": [],
                   "avoid": [], "food": [], "photo_spots": [], "tips": [], "cost_items": []}}
        if ticket is not None:
            p["甲"]["cost_items"] = [{"item": "门票", "type": "门票", "amount": float(ticket)}]
        return p

    def _plan(self):
        return {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "下午", "spot": "甲", "food": ""}]}]}

    # F-C1：种子层读回与 YAML 一致、带 source_url、支持别名、无城市不抛
    def test_seed_loader(self):
        from core.official_facts import load_city_facts, load_seed_facts
        from pipeline.decision import normalize_official

        facts = load_city_facts("北京", ["故宫博物院", "不存在的点"])
        self.assertIn("故宫博物院", facts)
        self.assertNotIn("不存在的点", facts)          # 层③：无来源不返回该点
        self.assertEqual(facts["故宫博物院"]["price"], 60)
        self.assertEqual(facts["故宫博物院"]["close_day"], "周一")
        self.assertEqual(facts["故宫博物院"]["source"], "seed")
        self.assertTrue(facts["故宫博物院"]["source_url"])   # 带来源
        # 别名归并：“故宫” 命中 “故宫博物院” 事实（输出键为请求名）
        via_alias = load_city_facts("北京", ["故宫"])
        self.assertEqual(via_alias["故宫"]["price"], 60)
        self.assertEqual(load_seed_facts("查无此城"), {})     # 缺文件静默返回空
        f = normalize_official(facts["故宫博物院"])
        self.assertEqual(f.price, 60.0)

    # F-C1/F-D4：官方价优先并带来源；无来源不得编造数字（预算退役后口径落在 catalog 详情）
    def test_official_price_wins_in_catalog(self):
        from pipeline.decision import build_catalog, build_decisions

        decs = build_decisions(profiles=self._profiles(), plan=self._plan(),
                               official_facts={"甲": {"price": 100, "source": "seed",
                                                      "source_url": "http://x/"}})
        det = build_catalog(decs)["poi"]["甲"]
        self.assertEqual(det["ticket_price"], 100.0)
        self.assertEqual(det["ticket_nature"], "官方确定")
        self.assertEqual(det["ticket_source"], "http://x/")
        # 无官方、无 UGC 票价 → 待核实（不填 0 冒充免费，不臆造票价）
        decs2 = build_decisions(profiles=self._profiles(), plan=self._plan())
        det2 = build_catalog(decs2)["poi"]["甲"]
        self.assertIsNone(det2["ticket_price"])
        self.assertEqual(det2["ticket_nature"], "待核实")

    # F-C3：官方覆盖评论（评论免费/评论高价两夹具）
    def test_arbitration_official_wins(self):
        from pipeline.decision import build_catalog, build_decisions

        # 评论说免费（门票 0）、官方收费 100 → 取官方
        decs = build_decisions(profiles=self._profiles(ticket=0), plan=self._plan(),
                               official_facts={"甲": {"price": 100, "source": "seed"}})
        det = build_catalog(decs)["poi"]["甲"]
        self.assertEqual(det["ticket_price"], 100.0)
        self.assertEqual(det["ticket_nature"], "官方确定")
        # 评论高价 120、官方 60 → 取官方
        decs2 = build_decisions(profiles=self._profiles(ticket=120), plan=self._plan(),
                                official_facts={"甲": {"price": 60, "source": "seed"}})
        det2 = build_catalog(decs2)["poi"]["甲"]
        self.assertEqual(det2["ticket_price"], 60.0)
        # 无官方时用已调研门票价作 UGC 参考
        decs3 = build_decisions(profiles=self._profiles(ticket=120), plan=self._plan())
        det3 = build_catalog(decs3)["poi"]["甲"]
        self.assertEqual(det3["ticket_price"], 120.0)
        self.assertEqual(det3["ticket_nature"], "UGC参考")

    # F-D11/#5：过期官方事实仍挂在决策对象上供 R9 警示（不静默丢弃）
    def test_expired_official_still_attached_for_r9(self):
        from pipeline.decision import normalize_official, official_expired, build_decisions

        expired = {"price": 80, "valid_until": "2020-01-01", "source": "seed"}
        self.assertTrue(official_expired(normalize_official(expired), "2026-09-05"))
        # 决策表仍保留该官方事实（供 R9 警示），不因过期而丢弃
        decs = build_decisions(
            candidates=[{"name": "甲", "reason": "地标"}],
            official_facts={"甲": expired}, plan=self._plan())
        d = next(x for x in decs if x.name == "甲")
        self.assertEqual(d.official.price, 80)
        self.assertIn("official", d.source_tags)

    # 接线契约：trip 靠 build_decisions(official_facts=) 把官方事实挂上决策对象
    def test_build_decisions_attaches_official(self):
        from pipeline.decision import build_decisions

        decs = build_decisions(
            candidates=[{"name": "故宫博物院", "reason": "地标"}],
            official_facts={"故宫博物院": {"price": 60, "close_day": "周一",
                                       "valid_until": "2099-01-01", "source_url": "http://x/"}},
            plan=self._plan())
        d = next(x for x in decs if x.name == "故宫博物院")
        self.assertEqual(d.official.price, 60.0)
        self.assertEqual(d.official.close_day, "周一")
        self.assertIn("official", d.source_tags)


class TestM2bGoldenCases(unittest.TestCase):
    """M2b：时间模型（day_weekdays/is_all_day）+ 结构化 Leg（F-D5，只留方式/耗时）
    + 动线折返（R5/F-D3）。全离线夹具，不打网络（强制 geo.available=False）。预算相关用例已退役。"""

    def test_day_weekdays_from(self):
        from pipeline.planner import day_weekdays_from

        self.assertEqual(day_weekdays_from("2026-09-05", 3), ["周六", "周日", "周一"])
        self.assertEqual(day_weekdays_from("2026/09/07", 1), ["周一"])
        self.assertEqual(day_weekdays_from(None, 3), [])
        self.assertEqual(day_weekdays_from("", 2), [])
        self.assertEqual(day_weekdays_from("乱码", 2), [])

    def test_is_all_day(self):
        from pipeline.planner import ALL_DAY_HOURS, is_all_day

        self.assertTrue(is_all_day({"duration_hours": ALL_DAY_HOURS}))
        self.assertTrue(is_all_day({"duration_hours": 8}))
        self.assertFalse(is_all_day({"duration_hours": 2}))
        self.assertTrue(is_all_day({"duration_hours": 1, "is_all_day": True}))
        self.assertFalse(is_all_day(None))

    def test_build_legs_offline(self):
        from unittest import mock

        from pipeline.planner import build_legs

        plan = {"days": [{"day": 1, "slots": [{"spot": "A"}, {"spot": "B"}, {"spot": "C"}]}]}
        locs = {"A": "116.30,39.90", "B": "116.40,39.99", "C": "116.50,39.90"}
        with mock.patch("core.geo.available", return_value=False):
            legs = build_legs("北京", "", plan, locs, travel_lines=["A->B: 地铁1号线"])
        self.assertEqual(len(legs), 2)                      # 无酒店坐标→仅相邻景点两段
        self.assertEqual([lg["from"] for lg in legs], ["A", "B"])
        self.assertEqual([lg["day"] for lg in legs], [1, 1])
        self.assertEqual(legs[0]["note"], "地铁1号线")
        self.assertTrue(all(lg["nature"] == "估算" for lg in legs))   # available False → 不声称实测
        self.assertTrue(all(lg["minutes"] for lg in legs))            # 每段都有耗时
        self.assertTrue(all("cost_min" not in lg for lg in legs))     # 报告不输出金额估算

    def test_r5_route_backtrack(self):
        from pipeline.qc import FAIL, PASS, SKIP, WARN, _r5_route

        no_locs = _r5_route({"days": []}, None)
        self.assertEqual(no_locs.status, SKIP)
        # 折返：A→B→A附近→C，实际路径远超直达
        zig = {"days": [{"day": 1, "slots": [{"spot": "A"}, {"spot": "B"},
                                            {"spot": "C"}, {"spot": "D"}]}]}
        zig_locs = {"A": "116.30,39.90", "B": "116.60,39.90",   # 东行
                    "C": "116.31,39.91", "D": "116.61,39.91"}   # 折回再东行
        self.assertEqual(_r5_route(zig, zig_locs).status, WARN)
        # 顺序不折返：一路东行
        smooth = {"days": [{"day": 1, "slots": [{"spot": "A"}, {"spot": "B"},
                                              {"spot": "C"}, {"spot": "D"}]}]}
        smooth_locs = {"A": "116.30,39.90", "B": "116.40,39.90",
                       "C": "116.50,39.90", "D": "116.60,39.90"}
        self.assertEqual(_r5_route(smooth, smooth_locs).status, PASS)

    def test_r1_all_day_exemption(self):
        from pipeline.qc import PASS, WARN, _r1_coverage

        # 全天型独占一天：单点不判“点过少”
        profiles = {"环球": {"duration_hours": 8, "best_time_slot": "全天"}}
        solo = {"days": [{"day": 1, "slots": [{"spot": "环球"}]}]}
        self.assertEqual(_r1_coverage(solo, profiles, 1).status, PASS)
        # 全天型与其他点同日→未独占 warn
        profiles2 = {"环球": {"duration_hours": 8}, "甲": {"duration_hours": 2}}
        shared = {"days": [{"day": 1, "slots": [{"spot": "环球"}, {"spot": "甲"}]}]}
        self.assertEqual(_r1_coverage(shared, profiles2, 1).status, WARN)


class TestM4aTripPlanCatalog(unittest.TestCase):
    """M4a：TripPlan 顶层契约（§6.11）+ catalog/RankItem/Intro 投影 + R11 无死链 / R12 同源。
    全离线夹具，不调 LLM、不打网络。"""

    def _decs(self):
        from pipeline.decision import build_decisions
        profiles = {
            "故宫": {"duration_hours": 4, "best_time_slot": "上午", "highlights": ["必上午来"],
                     "avoid": ["周一闭馆"], "open_time": "08:30", "cost_items": []},
            "颐和园": {"duration_hours": 5, "best_time_slot": "下午", "highlights": ["长廊"],
                      "cost_items": [{"item": "门票", "type": "门票", "amount": 30}]},
        }
        food_profiles = {"四季民福": {"cost_items": [{"type": "餐饮人均", "amount": 120, "n": 4}],
                                    "highlights": ["烤鸭"]}}
        official_facts = {"故宫": {"price": 60, "open_hours": "08:30-17:00", "close_day": "周一",
                                 "source_url": "http://dpm.org.cn"}}
        heat_rows = [{"spot": "故宫", "score": 0.9, "videos": 10, "trend": "长盛不衰"},
                     {"spot": "颐和园", "score": 0.7, "videos": 6, "trend": "平稳"},
                     {"spot": "四季民福", "score": 0.5, "videos": 3}]
        plan = {"days": [{"day": 1, "slots": [
            {"spot": "故宫", "slot": "上午"},
            {"spot": "颐和园", "slot": "下午"}]}]}
        legs = [{"from": "故宫", "to": "颐和园", "mode": "地铁", "minutes": 40, "km": 4.5,
                 "note": "", "nature": "估算", "day": 1}]
        decs = build_decisions(profiles=profiles, food_profiles=food_profiles, heat_rows=heat_rows,
                              official_facts=official_facts, plan=plan)
        return decs, plan, legs

    def test_build_catalog_rank_1to1(self):
        from pipeline.decision import build_catalog, to_rank_items
        decs, plan, legs = self._decs()
        cat = build_catalog(decs, legs=legs)
        self.assertIn("故宫", cat["poi"])
        self.assertIn("颐和园", cat["poi"])
        self.assertIn("四季民福", cat["food"])
        ranks = to_rank_items(decs, cat, kind="poi")
        self.assertEqual(ranks[0]["ref_id"], "故宫")     # 热度 0.9 > 0.7
        self.assertTrue(all(r["ref_id"] in cat["poi"] for r in ranks))   # 无死链

    def test_trip_plan_shape_and_same_source(self):
        import json

        from pipeline.decision import build_trip_plan
        decs, plan, legs = self._decs()
        d = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan,
                           legs=legs).to_dict()
        # M4b-2 起新增增量字段 snap（呈现快照）；预算退役后 §6.11 收敛为十一键
        self.assertEqual(
            {"meta", "intro", "itinerary", "heat_ranking", "food_ranking", "catalog",
             "quality", "decision_table", "plan_b", "to_verify", "appendix"},
            set(d.keys()) - {"snap"})
        json.dumps(d, ensure_ascii=False)               # 可序列化（API/MCP 直接用）
        # 票价同源：故宫官方 60 同时出现在 catalog 与 itinerary 块
        self.assertEqual(d["catalog"]["poi"]["故宫"]["ticket_price"], 60)
        blk = [b for day in d["itinerary"] for b in day["blocks"] if b["spot"] == "故宫"][0]
        self.assertEqual(blk["ticket_price"], 60)

    def test_r11_no_dead_link(self):
        import json

        from pipeline.decision import build_trip_plan
        from pipeline.qc import FAIL, PASS, SKIP, _r11_no_dead_link
        decs, plan, legs = self._decs()
        d = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan, legs=legs).to_dict()
        self.assertEqual(_r11_no_dead_link(d).status, PASS)
        self.assertEqual(_r11_no_dead_link(None).status, SKIP)
        bad = json.loads(json.dumps(d))
        bad["heat_ranking"].append({"ref_id": "幽灵点", "kind": "poi", "rank": 9})
        self.assertEqual(_r11_no_dead_link(bad).status, FAIL)

    def test_r12_triple_consistency(self):
        import json

        from pipeline.decision import build_trip_plan
        from pipeline.qc import FAIL, PASS, SKIP, _r12_triple_consistency
        decs, plan, legs = self._decs()
        d = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan, legs=legs).to_dict()
        self.assertEqual(_r12_triple_consistency(d).status, PASS)
        self.assertEqual(_r12_triple_consistency(None).status, SKIP)
        bad = json.loads(json.dumps(d))
        bad["itinerary"][0]["blocks"][0]["ticket_price"] = 999      # 篡改行程价≠详情
        self.assertEqual(_r12_triple_consistency(bad).status, FAIL)

    def test_intro_sections_and_facts_ref(self):
        from pipeline.decision import build_decisions, build_intro
        decs, plan, legs = self._decs()
        intro = build_intro(city="北京", days=1, decisions=decs, legs=legs)
        keys = {s["key"] for s in intro}
        self.assertTrue({"overview", "transport", "avoid", "spots"} <= keys)
        ov = next(s for s in intro if s["key"] == "overview")
        self.assertTrue(ov["facts_ref"])                 # 挂引用，不另造事实

    def test_run_gate_thread_trip_plan(self):
        from pipeline.decision import build_trip_plan
        from pipeline.qc import PASS, run_quality_gate
        decs, plan, legs = self._decs()
        tp = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan, legs=legs)
        # 不传 trip_plan → R11/R12 skip；传了→真实判定为 pass
        r1 = run_quality_gate(decisions=decs, plan=plan, profiles={}, days=1)
        self.assertEqual(r1.get("R11").status, "skip")
        r2 = run_quality_gate(decisions=decs, plan=plan, profiles={}, days=1, trip_plan=tp.to_dict())
        self.assertEqual(r2.get("R11").status, PASS)
        self.assertEqual(r2.get("R12").status, PASS)


class TestM4b1TripPlanWiring(unittest.TestCase):
    """M4b-1：决策层产出唯一 TripPlan + 定稿后 apply_trip_plan_checks 端到端激活 R11/R12。"""

    def _decs_plan_legs(self):
        from pipeline.decision import build_decisions
        profiles = {
            "故宫": {"duration_hours": 4, "best_time_slot": "上午", "avoid": ["周一闭馆", "需预约"], "cost_items": []},
            "颐和园": {"duration_hours": 5, "best_time_slot": "下午",
                      "cost_items": [{"item": "门票", "type": "门票", "amount": 30}]},
        }
        heat = [{"spot": "故宫", "score": 0.9, "videos": 8}, {"spot": "颐和园", "score": 0.7, "videos": 5}]
        official = {"故宫": {"price": 60, "source_url": "http://dpm.org.cn"}}
        plan = {"days": [{"day": 1, "slots": [
            {"spot": "故宫", "slot": "上午"}, {"spot": "颐和园", "slot": "下午"}]}]}
        legs = [{"from": "故宫", "to": "颐和园", "mode": "地铁", "minutes": 40, "km": 4.5,
                 "note": "", "nature": "估算", "day": 1}]
        decs = build_decisions(profiles=profiles, heat_rows=heat, official_facts=official, plan=plan)
        return decs, plan, legs

    def test_apply_trip_plan_checks_activates_r11_r12(self):
        from pipeline.decision import build_trip_plan
        from pipeline.qc import apply_trip_plan_checks, run_quality_gate
        decs, plan, legs = self._decs_plan_legs()
        report = run_quality_gate(decisions=decs, plan=plan, profiles={}, days=1)
        self.assertEqual(report.get("R11").status, "skip")   # 回炉阶段未接 trip_plan
        tp = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan, legs=legs)
        apply_trip_plan_checks(report, tp.to_dict())
        self.assertEqual(report.get("R11").status, "pass")
        self.assertEqual(report.get("R12").status, "pass")

    def test_apply_trip_plan_checks_dead_link_to_unresolved(self):
        from pipeline.decision import build_trip_plan
        from pipeline.qc import apply_trip_plan_checks, run_quality_gate
        decs, plan, legs = self._decs_plan_legs()
        report = run_quality_gate(decisions=decs, plan=plan, profiles={}, days=1)
        tp = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan, legs=legs).to_dict()
        tp["heat_ranking"].append({"ref_id": "幽灵点", "kind": "poi", "rank": 9})
        apply_trip_plan_checks(report, tp)
        self.assertEqual(report.get("R11").status, "fail")
        self.assertTrue(any("R11" in u for u in report.unresolved))

    def test_poi_detail_pitfalls_fallback_from_avoid(self):
        from pipeline.decision import build_catalog, build_decisions
        decs, plan, legs = self._decs_plan_legs()
        cat = build_catalog(decs)["poi"]
        pit = [x["text"] for x in cat["故宫"]["pitfalls"]]
        self.assertIn("周一闭馆", pit)     # 未传 pitfall_by_spot 时从 avoid 回退


class TestM4b2TripRender(unittest.TestCase):
    """M4b-2：TripPlan 只读双渲染（0~11 报告 IA + 榜单→详情库锦点下钻 + 表达层零计算）。"""

    def _plan_dict(self):
        from pipeline.decision import build_decisions, build_trip_plan
        profiles = {
            "故宫": {"duration_hours": 4, "best_time_slot": "上午", "highlights": ["必上午来"],
                     "avoid": ["周一闭馆"], "open_time": "08:30", "cost_items": [],
                     "tips": ["带身份证"], "food": ["四季民福"]},
            "颐和园": {"duration_hours": 5, "best_time_slot": "下午", "highlights": ["长廊"],
                      "cost_items": [{"item": "门票", "type": "门票", "amount": 30}]},
        }
        food_profiles = {"四季民福": {"cost_items": [{"type": "餐饮人均", "amount": 120, "n": 4}],
                                    "highlights": ["烤鸭"], "tips": ["提前取号"]}}
        official = {"故宫": {"price": 60, "open_hours": "08:30-17:00", "close_day": "周一",
                           "source_url": "http://dpm.org.cn"}}
        heat = [{"spot": "故宫", "score": 0.9, "videos": 10, "likes": 500, "comments": 80,
                 "trend": "长盛不衰", "sentiment": "好评多"},
                {"spot": "颐和园", "score": 0.7, "videos": 6, "trend": "平稳"}]
        plan = {"days": [{"day": 1, "slots": [
            {"spot": "故宫", "slot": "上午"},
            {"spot": "颐和园", "slot": "下午"}], "summary_note": ""}]}
        legs = [{"day": 1, "from": "故宫", "to": "颐和园", "mode": "地铁", "minutes": 40,
                 "km": 4.5, "nature": "估算", "note": ""}]
        decs = build_decisions(profiles=profiles, food_profiles=food_profiles,
                               heat_rows=heat, official_facts=official, plan=plan)
        snap = {
            "user_spots": ["故宫"],
            "overview": {"days": 1, "spots": 2, "slots": 2, "foods": 1,
                         "highlights": 2, "pitfalls": 1},
            "pitfall": [{"claim": "周一闭馆", "conf_level": "多源一致", "n_sources": 2,
                         "quote": "周一别去", "source": "http://x"}],
            "digests": {"故宫": {"verdict": "值得", "positive": "壮观", "negative": "人多",
                              "quotes": ["人超多"]}},
            "legs": legs, "locs": {"故宫": "116.397,39.918"}, "geo_on": False,
            "summary_note": "测试规划说明",
            "profiles": profiles, "foods": food_profiles, "heat": heat,
        }
        meta = {"city": "北京", "days": 1, "stay": "前门",
                "preference_mode": "经典", "generated_at": "2026-09-08T12:00:00",
                "collect_mode": "kernel-only", "facts_cutoff": "2026-09-08"}
        quality = {"score": 92, "repair_rounds": 1, "unresolved": [],
                   "checks": [{"rule_id": "R9", "name": "时效核验", "status": "pass",
                               "actual": "0", "note": ""}]}
        return build_trip_plan(meta=meta, decisions=decs, plan=plan,
                               quality=quality, legs=legs, snap=snap).to_dict()

    def test_markdown_ia_and_drilldown(self):
        from pipeline.trip_render import DISCLAIMER, render_markdown
        md = render_markdown(self._plan_dict())
        for sec in ("# 《北京》1 天行程规划", "## 行程概览", "## 质量分卡", "## 攻略介绍",
                    "## 选点决策表", "## 第 1 天", "## 分段交通",
                    "## 避坑专题", "## 热度榜", "## 美食榜", "## 详情库"):
            self.assertIn(sec, md)
        # 0 需求回显：生成时间（字符串 ISO 不崩溃，回归 M4b-2 修复）+ 来源/路线依据行
        self.assertIn("生成时间：2026-09-08 12:00", md)
        self.assertIn("数据来源：2 个景点 + 1 家餐厅", md)
        self.assertIn("路线依据：LLM 交通估算", md)
        self.assertIn("规划说明：测试规划说明", md)
        # 8/9→10 榜单锦点：锚点与详情库一一对应
        self.assertIn("[详情](#spot-故宫)", md)
        self.assertIn("[详情](#food-四季民福)", md)
        self.assertIn('<a id="spot-故宫">', md)
        self.assertIn('<a id="food-四季民福">', md)
        # 票价同源在 MD 的唯一出口（catalog 权威价）
        self.assertIn("门票：60 元（官方确定）", md)
        # 旧详情卡特性不丢：贴士/美食/热度证据
        self.assertIn("贴士：带身份证", md)
        self.assertIn("美食：四季民福", md)
        self.assertIn("热度证据：视频 10 条", md)
        self.assertIn(DISCLAIMER.splitlines()[0][2:], md)

    def test_renderers_are_readonly(self):
        import json
        from pipeline.trip_render import render_html, render_markdown
        d = self._plan_dict()
        before = json.dumps(d, ensure_ascii=False, sort_keys=True)
        render_markdown(d)
        render_html(d)
        self.assertEqual(before, json.dumps(d, ensure_ascii=False, sort_keys=True))

    def test_html_projects_template_context(self):
        from pipeline.trip_render import render_html
        html = render_html(self._plan_dict())
        self.assertIn("北京", html)
        self.assertIn("四季民福", html)
        self.assertIn("规划说明：测试规划说明", html)
        self.assertIn("60", html)   # catalog 同源票价进模板 context


class TestM4b3FoodRank(unittest.TestCase):
    """M4b-3：美食榜独立口径（F-F2）——多维可复算排序 + 样本下限显式标注。"""

    def _decs_cat(self):
        from pipeline.decision import build_catalog, build_decisions
        food_profiles = {
            "四季民福": {"cost_items": [{"type": "餐饮人均", "amount": 120, "n": 4}],
                        "highlights": ["烤鸭"], "avoid": ["需排号"],
                        "queue_risk": "高", "tags": ["老字号"]},
            "护国寺小吃": {"cost_items": [{"type": "餐饮人均", "amount": 25, "n": 6}],
                        "highlights": ["豌豆黄"], "queue_risk": "低", "tags": ["本地"]},
        }
        profiles = {"故宫": {"duration_hours": 4, "best_time_slot": "上午", "cost_items": []}}
        heat = [{"spot": "故宫", "score": 0.9, "videos": 8}]
        plan = {"days": [{"day": 1, "slots": [{"spot": "故宫", "slot": "上午", "cost": 60}]}]}
        decs = build_decisions(profiles=profiles, food_profiles=food_profiles,
                               heat_rows=heat, official_facts={}, plan=plan)
        return decs, build_catalog(decs)

    def test_food_score_reproducible(self):
        from pipeline.decision import _FOOD_WEIGHTS, food_rank_score, food_score_breakdown
        decs, cat = self._decs_cat()
        det = cat["food"]["四季民福"]
        bd = food_score_breakdown(det)
        self.assertEqual(set(bd), set(_FOOD_WEIGHTS))       # 各维齐且可复算
        self.assertEqual(food_rank_score(det),
                         round(sum(_FOOD_WEIGHTS[k] * v for k, v in bd.items()), 3))

    def test_food_rank_order_and_breakdown(self):
        from pipeline.decision import to_food_rank_items
        decs, cat = self._decs_cat()
        ranks = to_food_rank_items(decs, cat)
        self.assertTrue(all(r["ref_id"] in cat["food"] for r in ranks))   # R11 无死链
        # 样本足（6 样本、低排队）的小吃 > 排队高风险的：排序不随热度为 0 退化
        self.assertEqual(ranks[0]["ref_id"], "护国寺小吃")
        self.assertTrue(ranks[0]["score"] > ranks[1]["score"])
        self.assertTrue(ranks[0]["score_breakdown"])

    def test_food_sample_min_note(self):
        from pipeline.decision import build_trip_plan
        decs, cat = self._decs_cat()
        plan = {"days": [{"day": 1, "slots": [{"spot": "故宫", "slot": "上午", "cost": 60}]}]}
        d1 = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs,
                             plan=plan, food_min_samples=5).to_dict()
        self.assertIn("低于样本下限 5", d1["snap"]["food_sample_note"])
        d2 = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs,
                             plan=plan).to_dict()   # 不传下限→不写标注（零回归）
        self.assertNotIn("food_sample_note", d2["snap"])

    def test_md_food_rank_and_r11(self):
        from pipeline.decision import build_trip_plan
        from pipeline.qc import PASS, _r11_no_dead_link
        from pipeline.trip_render import render_markdown
        decs, cat = self._decs_cat()
        plan = {"days": [{"day": 1, "slots": [{"spot": "故宫", "slot": "上午", "cost": 60}]}]}
        d = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs,
                            plan=plan, food_min_samples=5).to_dict()
        md = render_markdown(d)
        self.assertIn("推荐分", md)
        self.assertIn("低于样本下限 5", md)
        self.assertIn("[详情](#food-", md)
        self.assertEqual(_r11_no_dead_link(d).status, PASS)


class TestBugfixDuplicateSpot(unittest.TestCase):
    """用户报障：大同行程第 1 天上午去了九龙壁、第 2 天下午又去一次。

    修复：_normalize_plan 跨天/同天去重（保留首次出现）+ R1 按去重点位计数 + 报告明示剔除。
    """

    def test_drops_cross_day_duplicate(self):
        from pipeline.planner import _normalize_plan

        data = {"summary_note": "", "days": [
            {"day": 1, "slots": [{"slot": "上午", "spot": "九龙壁"},
                                 {"slot": "下午", "spot": "云冈石窟"}]},
            {"day": 2, "slots": [{"slot": "上午", "spot": "善化寺"},
                                 {"slot": "下午", "spot": "九龙壁"}]},
        ]}
        plan = _normalize_plan(data, {"九龙壁", "云冈石窟", "善化寺"}, days=2)
        spots = [s["spot"] for d in plan["days"] for s in d["slots"]]
        self.assertEqual(spots, ["九龙壁", "云冈石窟", "善化寺"])   # 首次出现保留，重复丢弃
        self.assertEqual(plan["duplicate_drops"], ["九龙壁（第2天下午）"])

    def test_same_day_duplicate_and_no_dup_regression(self):
        from pipeline.planner import _normalize_plan

        # 同一天内重复也剔除
        dup = _normalize_plan({"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "甲"}, {"slot": "下午", "spot": "甲"}]}]}, {"甲"}, days=1)
        self.assertEqual(len(dup["days"][0]["slots"]), 1)
        self.assertEqual(dup["duplicate_drops"], ["甲（第1天下午）"])
        # 无重复时零回归：duplicate_drops 为空，旧键不变
        clean = _normalize_plan({"summary_note": "n", "days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "甲"}, {"slot": "下午", "spot": "乙"}]}]}, {"甲", "乙"}, days=1)
        self.assertEqual(clean["duplicate_drops"], [])
        self.assertEqual(clean["summary_note"], "n")
        self.assertEqual([s["spot"] for s in clean["days"][0]["slots"]], ["甲", "乙"])

    def test_r1_counts_unique_spots_and_reports_drops(self):
        from pipeline.qc import WARN, _r1_coverage

        plan = {"days": [{"day": 1, "slots": [{"spot": "甲"}, {"spot": "乙"}]},
                         {"day": 2, "slots": [{"spot": "丙"}]}],
                "duplicate_drops": ["甲（第2天上午）"]}
        c = _r1_coverage(plan, {"甲": {}, "乙": {}, "丙": {}}, days=2)
        self.assertEqual(c.status, WARN)
        self.assertIn("入选 3 点", c.actual)         # 去重后 3 点，不是 4 槽
        self.assertIn("已剔除重复排入 1 处", c.actual)
        self.assertIn("甲（第2天上午）", c.actual)      # 剔除位置对用户可见
        self.assertIn("每点全程只应排一次", c.note)


class TestDayCoverageGuard(unittest.TestCase):
    """P0：用户要 3 天就必须给 3 天——实测出现过"只排 2 天而 R1 报 pass"。

    三层防线：提示词（PLAN_SYSTEM 规则 1）→ 回炉（_coverage_issues）→ 门禁（R1 FAIL）。"""

    def test_r1_fails_when_day_missing(self):
        """要求 3 天但只输出 2 天：即使点位总数达标也必须 FAIL。"""
        from pipeline.qc import FAIL, _r1_coverage

        prof = {"甲": {}, "乙": {}, "丙": {}, "丁": {}}
        short = {"days": [{"day": 1, "slots": [{"spot": "甲"}, {"spot": "乙"}]},
                          {"day": 2, "slots": [{"spot": "丙"}, {"spot": "丁"}]}]}
        c = _r1_coverage(short, prof, days=3)
        self.assertEqual(c.status, FAIL)
        self.assertIn("要求 3 天", c.actual)
        self.assertIn("仅输出 2 天", c.actual)
        self.assertIn("全部 3 天", c.fix)          # 修正指令可直接喂回炉
        self.assertIn("不得空整天", c.fix)

    def test_r1_fails_when_day_empty(self):
        """天数够但某天零槽位：同样 FAIL，并点名是第几天。"""
        from pipeline.qc import FAIL, _r1_coverage

        prof = {"甲": {}, "乙": {}, "丙": {}}
        hollow = {"days": [{"day": 1, "slots": [{"spot": "甲"}, {"spot": "乙"}]},
                           {"day": 2, "slots": []},
                           {"day": 3, "slots": [{"spot": "丙"}]}]}
        c = _r1_coverage(hollow, prof, days=3)
        self.assertEqual(c.status, FAIL)
        self.assertIn("第 2 天无任何安排", c.actual)

    def test_r1_passes_when_days_sparse_but_complete(self):
        """素材不足时每天 1 点也合法：天数齐就不该因"不够密"而 FAIL。"""
        from pipeline.qc import PASS, _r1_coverage

        prof = {"甲": {}, "乙": {}, "丙": {}}
        sparse = {"days": [{"day": 1, "slots": [{"spot": "甲"}]},
                           {"day": 2, "slots": [{"spot": "乙"}]},
                           {"day": 3, "slots": [{"spot": "丙"}]}]}
        c = _r1_coverage(sparse, prof, days=3)
        self.assertEqual(c.status, PASS)          # min_slots 按档案数放宽到 3
        self.assertIn("入选 3 点", c.actual)

    def test_coverage_issues_flags_missing_days(self):
        """回炉层：缺天要进 issues，否则不会触发重试就直接出报告了。"""
        from pipeline.planner import _coverage_issues

        prof = {"甲": {}, "乙": {}}
        short = {"days": [{"day": 1, "slots": [{"spot": "甲", "slot": "上午"},
                                               {"spot": "乙", "slot": "下午"}]}]}
        issues = _coverage_issues(short, prof, days=3)
        self.assertTrue(any("行程天数不完整" in i for i in issues))
        self.assertTrue(any("要求 3 天" in i for i in issues))
        # 天数齐时零回归：不因新检查凭空多报问题
        full = {"days": [{"day": 1, "slots": [{"spot": "甲", "slot": "上午"}]},
                         {"day": 2, "slots": [{"spot": "乙", "slot": "下午"}]}]}
        self.assertFalse(any("天数不完整" in i for i in _coverage_issues(full, prof, days=2)))

    def test_plan_system_demands_all_days(self):
        """提示词层：硬约束必须写在规则里（LLM 会违反，但没写就更没依据）。"""
        from pipeline.planner import PLAN_SYSTEM

        self.assertIn("必须输出全部 N 天", PLAN_SYSTEM)
        self.assertIn("严禁只输出前几天", PLAN_SYSTEM)


class TestDetailCardAndTicketLine(unittest.TestCase):
    """P1/P2：详情库空壳卡与"待核实（待核实）"重复。"""

    def test_ticket_line_no_duplicate_todo(self):
        """nature 兜底成"待核实"时不得再套一层括号（实测一份报告 15 处）。"""
        from pipeline.trip_render import _ticket_line

        self.assertEqual(_ticket_line({}), "- 门票：待核实")
        self.assertEqual(_ticket_line({"ticket_nature": "待核实"}), "- 门票：待核实")
        self.assertEqual(_ticket_line({"ticket_nature": "官方", "ticket_source": "u1"}),
                         "- 门票：待核实（官方），来源：u1")
        self.assertEqual(_ticket_line({"ticket_price": 60, "ticket_nature": "官方"}),
                         "- 门票：60 元（官方）")
        self.assertEqual(_ticket_line({"ticket_price": 0, "ticket_nature": "免费"}),
                         "- 门票：0 元（免费）")       # 显式 0 是"免费"，不是待核实

    def test_has_content_judges(self):
        """空壳判定：无任何实质字段才算空；ticket_price=0 与 guide_note 都算有内容。"""
        from pipeline.trip_render import _has_food_content, _has_poi_content

        self.assertFalse(_has_poi_content({}, {}, None, None))
        self.assertTrue(_has_poi_content({"summary": "s"}, {}, None, None))
        self.assertTrue(_has_poi_content({"ticket_price": 0}, {}, None, None))
        self.assertTrue(_has_poi_content({}, {"tips": ["t"]}, None, None))
        self.assertTrue(_has_poi_content({}, {"food": ["f"]}, None, None))
        self.assertTrue(_has_poi_content({}, {}, {"videos": 5}, None))
        self.assertTrue(_has_poi_content({}, {}, None, {"good": []}))
        self.assertTrue(_has_poi_content({"guide_note": "攻略提到"}, {}, None, None))
        self.assertFalse(_has_food_content({}, {}, None))
        self.assertTrue(_has_food_content({"avg_price": 30}, {}, None))
        self.assertTrue(_has_food_content({"guide_note": "n"}, {}, None))

    def _tp(self, poi, food):
        return {"meta": {"city": "成都", "days": 3}, "snap": {}, "intro": {},
                "catalog": {"poi": poi, "food": food}, "itinerary": [],
                "heat_ranking": [], "food_ranking": [], "quality": {},
                "decision_table": [], "plan_b": [], "to_verify": [], "appendix": {}}

    def test_empty_card_renders_note_and_keeps_anchor(self):
        """空档案 → 一句话说明；锚点必须保留（否则榜单 [详情] 变 R11 死链）。"""
        from pipeline.trip_render import render_markdown

        md = render_markdown(self._tp(
            {"甲": {}, "乙": {"summary": "有内容", "highlights": ["h"]}},
            {"丙": {}, "丁": {"avg_price": 30, "price_samples_n": 2}}))
        self.assertIn('<a id="spot-甲"></a>', md)              # 锚点保留
        self.assertIn("暂无档案数据", md)
        self.assertIn("有内容", md)                              # 有档案的正常渲染
        self.assertNotIn("待核实（待核实）", md)
        self.assertNotIn("建议时长：见要点｜最佳时段：—\n\n<a id=\"spot-乙\"", md)
        self.assertIn('<a id="food-丙"></a>', md)
        self.assertIn("人均：30 元", md)


class TestGuideEvidenceFoodRank(unittest.TestCase):
    """P3：攻略层实证补进 catalog，让撞风控没采到视频的餐厅在美食榜有区分度。"""

    def test_apply_guide_evidence_only_fills_empty(self):
        from pipeline.decision import apply_guide_evidence

        cat = {"poi": {"甲": {"summary": "实测"}}, "food": {"莲芳蹄花店": {}, "乙": {}}}
        guide = {"guide_candidates": [
            {"name": "甲", "category": "景点", "heat": "高", "note": "攻略说值得"},
            {"name": "莲芳蹄花店", "category": "美食", "heat": "高", "note": "蹄花汤浓"},
            {"name": "没匹配上的店", "category": "美食", "heat": "中", "note": "x"}]}
        out = apply_guide_evidence(cat, guide)
        self.assertEqual(out["food"]["莲芳蹄花店"]["guide_heat"], "高")
        self.assertEqual(out["food"]["莲芳蹄花店"]["guide_note"], "蹄花汤浓")
        self.assertEqual(out["poi"]["甲"]["guide_heat"], "高")
        self.assertEqual(out["poi"]["甲"]["summary"], "实测")     # 不覆盖已有实测数据
        self.assertNotIn("guide_note", out["food"]["乙"])          # 未被提及的不注入
        self.assertEqual(cat["food"]["莲芳蹄花店"], {})            # 入参未被改
        # 无攻略数据时原样返回（kernel-only / 攻略层未启用零回归）
        self.assertIs(apply_guide_evidence(cat, None), cat)
        self.assertIs(apply_guide_evidence(cat, {"guide_candidates": []}), cat)

    def test_fuzzy_mention_matches_substring(self):
        from pipeline.decision import _fuzzy_mention

        by = {"莲芳蹄花": {"heat": "高"}}
        self.assertEqual(_fuzzy_mention("莲芳蹄花店", by)["heat"], "高")
        self.assertIsNone(_fuzzy_mention("甲", by))            # 短名不参与包含匹配
        self.assertIsNone(_fuzzy_mention("无关店名", by))

    def test_food_score_uses_guide_heat_with_discount(self):
        """攻略提及按 0.7 折扣计入热度维；实测热度更高时不被拉低。"""
        from pipeline.decision import GUIDE_HEAT_DISCOUNT, food_rank_score, food_score_breakdown

        empty = food_score_breakdown({})
        hi = food_score_breakdown({"guide_heat": "高"})
        mid = food_score_breakdown({"guide_heat": "中"})
        lo = food_score_breakdown({"guide_heat": "低"})
        self.assertEqual(empty["heat"], 0.0)
        self.assertAlmostEqual(hi["heat"], round(1.0 * GUIDE_HEAT_DISCOUNT, 2))
        self.assertGreater(hi["heat"], mid["heat"])
        self.assertGreater(mid["heat"], lo["heat"])
        real = food_score_breakdown({"heat_score": 0.95, "guide_heat": "低"})
        self.assertEqual(real["heat"], 0.95)                  # 实测优先
        # 推荐分因此有了区分度（此前 5 家全并列）
        self.assertAlmostEqual(food_rank_score({}), 0.245, places=3)
        self.assertGreater(food_rank_score({"guide_heat": "高"}), food_rank_score({}))
        self.assertGreater(food_rank_score({"guide_heat": "中"}), food_rank_score({}))

    def test_food_ranking_order_by_guide_heat(self):
        """榜单排序：全空档案时按攻略提及热度分先后，不再全部并列。"""
        from pipeline.decision import apply_guide_evidence, food_rank_score

        cat = {"poi": {}, "food": {"高赞店": {}, "中等店": {}, "低提店": {}, "无关店": {}}}
        guide = {"guide_candidates": [
            {"name": "低提店", "heat": "低", "note": ""},
            {"name": "高赞店", "heat": "高", "note": "必吃"},
            {"name": "中等店", "heat": "中", "note": ""}]}
        out = apply_guide_evidence(cat, guide)
        scores = {k: food_rank_score(v) for k, v in out["food"].items()}
        self.assertGreater(scores["高赞店"], scores["中等店"])
        self.assertGreater(scores["中等店"], scores["低提店"])
        self.assertGreater(scores["低提店"], scores["无关店"])

    def test_render_shows_guide_heat_and_note(self):
        """渲染层：美食榜标"攻略提及热度"，详情卡标来源（不与实测混淆）。"""
        from pipeline.trip_render import render_markdown

        tp = {"meta": {"city": "成都", "days": 3}, "snap": {}, "intro": {},
              "catalog": {"poi": {}, "food": {"莲芳蹄花店": {
                  "guide_heat": "高", "guide_note": "蹄花汤浓白"}}},
              "itinerary": [], "heat_ranking": [],
              "food_ranking": [{"ref_id": "莲芳蹄花店", "rank": 1, "score": 0.35}],
              "budget": {}, "quality": {}, "decision_table": [], "plan_b": [],
              "to_verify": [], "appendix": {}}
        md = render_markdown(tp)
        self.assertIn("攻略提及热度高", md)
        self.assertIn("- 攻略提及：蹄花汤浓白（来自城市高赞攻略视频，非独立实测）", md)


class TestRouteAdviceDegrade(unittest.TestCase):
    """P4：公交方案劣化时如实提醒（实测 2.1km 给出 3 次换乘+步行 1289 米+46 分钟）。"""

    # 春熙路→宽窄巷子约 2.5km：落在 1.5~3km 区间，应并列给步行
    A, B = "104.077774,30.655544", "104.053307,30.663869"

    def _run(self, transit_walk, transit_dur, taxi_dur, walk_dur="1500"):
        from core import geo

        def fake_get(path, params):
            if path == "/direction/transit/integrated":
                return {"status": "1", "route": {"transits": [{
                    "duration": str(transit_dur), "cost": "3",
                    "walking_distance": str(transit_walk),
                    "segments": [{"bus": {"buslines": [{
                        "name": "地铁2号线", "via_num_stops": "",
                        "departure_stop": {"name": "春熙路"},
                        "arrival_stop": {"name": "通惠门"}}]}}]}]}}
            if path == "/direction/walking":
                return {"status": "1", "route": {"paths": [
                    {"duration": walk_dur, "distance": "2100"}]}}
            if path == "/direction/driving":
                return {"status": "1", "route": {"taxi_cost": "12", "paths": [
                    {"duration": str(taxi_dur)}]}}
            return None

        orig = (geo._amap_get, geo.AMAP_API_KEY)
        geo._amap_get, geo.AMAP_API_KEY = fake_get, "fake"
        geo._route_cache.clear()
        try:
            return geo.route_advice(self.A, self.B, "成都")
        finally:
            geo._amap_get, geo.AMAP_API_KEY = orig
            geo._route_cache.clear()

    def test_degrade_hint_when_walk_too_far(self):
        out = self._run(transit_walk=1289, transit_dur=2760, taxi_dur=1680)
        self.assertIn("公交约46分钟·3元", out)
        self.assertIn("地铁2号线(春熙路→通惠门)", out)   # 缺站数不留半截括号
        self.assertNotIn(", 站)", out)
        self.assertIn("含步行约1289米", out)
        self.assertIn("步行约25分钟", out)              # 1.5~3km 并列步行
        self.assertIn("打车约28分钟·约12元", out)
        self.assertIn("公交换乘与步行偏多", out)          # 劣化提醒

    def test_no_hint_when_reasonable(self):
        """方案合理时不加提醒：步行段短、耗时与打车相当就保持原样输出。"""
        out = self._run(transit_walk=300, transit_dur=1200, taxi_dur=1080)
        self.assertIn("公交约20分钟", out)
        self.assertNotIn("公交换乘与步行偏多", out)

    def test_hint_when_transit_much_slower_than_taxi(self):
        """步行段不长但耗时是打车的两倍多（超 1.6 倍阈值）也要提醒。"""
        out = self._run(transit_walk=400, transit_dur=3600, taxi_dur=1200)
        self.assertIn("公交约60分钟", out)
        self.assertIn("打车约20分钟", out)
        self.assertIn("公交换乘与步行偏多", out)


class TestBugfixTicketSameSource(unittest.TestCase):
    """用户报障：悬空寺门票 899 元（评论“携程899直接买票”被当成门票）。

    修复：pick_ticket_price 成为门票唯一口径（剔第三方代抢/套餐加价、多条取最低），
    catalog 详情与行程槽位共用；预算退役后 R12 判行程/详情两处同源。
    """

    def _items(self):
        return [{"item": "门票", "type": "门票", "amount": 15.0},
                {"item": "携程899直接买票，全程速通", "type": "门票", "amount": 899.0},
                {"item": "登临费", "type": "门票", "amount": 100.0}]

    def test_pick_ticket_price_rules(self):
        from pipeline.planner import pick_ticket_price

        self.assertEqual(pick_ticket_price(self._items()), 15.0)   # 899 代抢剔除，多条取最低
        self.assertEqual(pick_ticket_price([{"item": "门票", "type": "门票", "amount": 0}]), 0.0)  # 免费
        self.assertIsNone(pick_ticket_price([{"item": "讲解服务", "type": "门票", "amount": 50}]))
        self.assertIsNone(pick_ticket_price([]))
        self.assertIsNone(pick_ticket_price(None))

    def test_normalize_profile_drops_third_party_ticket(self):
        from pipeline.planner import _normalize_profile

        p = _normalize_profile({"cost_items": self._items()})
        amts = [c["amount"] for c in p["cost_items"] if c["type"] == "门票"]
        self.assertEqual(amts, [15.0, 100.0])       # 899 代抢项不进档案

    def test_catalog_and_itinerary_same_price(self):
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.qc import PASS, _r12_triple_consistency

        profiles = {"悬空寺": {"duration_hours": 2, "best_time_slot": "晚上", "highlights": [],
                             "avoid": [], "food": [], "photo_spots": [], "tips": [],
                             "cost_items": self._items()}}
        plan = {"summary_note": "", "days": [{"day": 1, "slots": [
            {"slot": "晚上", "spot": "悬空寺"}]}]}
        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan)
        tp = build_trip_plan(meta={"city": "大同", "days": 1}, decisions=decs, plan=plan).to_dict()
        det = tp["catalog"]["poi"]["悬空寺"]
        self.assertEqual(det["ticket_price"], 15.0)               # 899 代抢价不再出口
        self.assertEqual(det["ticket_nature"], "UGC参考")
        blk = [b for day in tp["itinerary"] for b in day["blocks"] if b["spot"] == "悬空寺"][0]
        self.assertEqual(blk["ticket_price"], 15.0)               # 行程槽位与详情同源
        self.assertEqual(_r12_triple_consistency(tp).status, PASS)

    def test_r12_two_source_split_detected(self):
        from pipeline.qc import FAIL, PASS, _r12_triple_consistency

        # 预算字段已退役：即使残留 budget 键也不参与 R12（只判行程 ↔ 详情两处）
        tp = {"catalog": {"poi": {"甲": {"ticket_price": 15.0}}},
              "itinerary": [{"blocks": [{"spot": "甲", "ticket_price": 15.0}]}],
              "budget": {"ticket_by_spot": {"甲": 899}}}
        self.assertEqual(_r12_triple_consistency(tp).status, PASS)
        # 行程槽位价被篡改、与详情分裂 → FAIL，具体数字必须可展示
        tp["itinerary"][0]["blocks"][0]["ticket_price"] = 899.0
        c = _r12_triple_consistency(tp)
        self.assertEqual(c.status, FAIL)
        self.assertIn("899", c.actual)
        self.assertIn("同一 catalog 票价", c.fix)


class TestVideoQualityGate(unittest.TestCase):
    """M6-A 质量闸：门槛判定 / 质量分 / 候选池预筛 / 自动降档 / 详情页候补校验。"""

    def _item(self, like=50000, days_ago=10, duration=120.0, desc="三天两夜攻略", **kw):
        from datetime import datetime, timedelta

        from core.models import VideoItem
        return VideoItem(video_id=kw.pop("vid", "v1"), url=kw.pop("url", "https://x/1"),
                         description=desc, like_count=like, duration=duration,
                         publish_time=(datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
                         **kw)

    def test_reject_reason_rules(self):
        """严格档（默认）：点赞≥2000、一年内、时长≥30 秒、营销号直接拒。"""
        from core.quality import (REJECT_LIKES, REJECT_MARKETING, REJECT_OLD,
                                  REJECT_SHORT, reject_reason)

        self.assertEqual(reject_reason(self._item(like=50000), "strict"), "")
        self.assertEqual(reject_reason(self._item(like=1500), "strict"), REJECT_LIKES)
        self.assertEqual(reject_reason(self._item(days_ago=500), "strict"), REJECT_OLD)
        self.assertEqual(reject_reason(self._item(duration=12.0), "strict"), REJECT_SHORT)
        self.assertEqual(reject_reason(self._item(desc="团购优惠点击左下角"), "strict"),
                         REJECT_MARKETING)
        # 降档后同一条可通过（normal：≥500 / 两年内）
        self.assertEqual(reject_reason(self._item(like=1500, days_ago=500), "normal"), "")
        # loose 档不限时效与时长
        self.assertEqual(reject_reason(self._item(like=150, days_ago=1200, duration=5.0), "loose"), "")

    def test_missing_metrics_never_reject(self):
        """指标缺失（旧缓存 JSON / 接口改版）时该维度不生效，绝不误杀存量数据。"""
        from core.quality import reject_reason

        it = self._item()
        it.like_count, it.publish_time, it.duration = None, None, None
        self.assertEqual(reject_reason(it, "strict"), "")

    def test_relax_level_chain(self):
        from core.quality import relax_level

        self.assertEqual(relax_level("strict"), "normal")
        self.assertEqual(relax_level("normal"), "loose")
        self.assertIsNone(relax_level("loose"))     # 到底了：只能如实标注素材不足

    def test_quality_score_monotonic_and_penalty(self):
        """质量分：点赞单调递增、营销号砍半、播放量/收藏/新鲜度也参与（不是只看点赞）。"""
        from core.quality import WEIGHTS, quality_breakdown, video_quality_score

        lo = video_quality_score(self._item(like=1000))
        hi = video_quality_score(self._item(like=200000))
        self.assertGreater(hi, lo)
        mkt = video_quality_score(self._item(like=200000, desc="团购推广合作"))
        self.assertLess(mkt, hi * 0.6)              # 刷量也进不了前排
        base = video_quality_score(self._item(like=50000, collect_count=0, days_ago=700))
        rich = video_quality_score(self._item(like=50000, collect_count=20000, days_ago=5))
        self.assertGreater(rich, base)
        # 播放量也计入（实跑已确认 statistics 下发 play_count）
        no_play = video_quality_score(self._item(like=50000, play_count=None))
        big_play = video_quality_score(self._item(like=50000, play_count=3000000))
        self.assertGreater(big_play, no_play)
        bd = quality_breakdown(self._item(like=50000, play_count=3000000))
        self.assertEqual(set(bd) - {"score", "marketing"},
                         {"play", "likes", "collect", "comments", "fresh"})
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)   # 权重归一，分数可复算
        self.assertAlmostEqual(bd["score"],
                               sum(bd[k] * WEIGHTS[k] for k in WEIGHTS), places=4)

    def test_screen_pool_picks_and_reports(self):
        """预筛：picked 是首批深采名单，kept 多出的作候补，淘汰按原因计数。"""
        from core.quality import screen_pool

        pool = ([{"video_id": f"h{i}", "url": f"u_h{i}", "like_count": 50000} for i in range(4)]
                + [{"video_id": f"l{i}", "url": f"u_l{i}", "like_count": 300} for i in range(6)])
        res = screen_pool(pool, 3, level="strict")
        self.assertEqual(res["level"], "strict")            # 达标够，不降档
        self.assertEqual([c["url"] for c in res["picked"]], ["u_h0", "u_h1", "u_h2"])
        self.assertEqual(len(res["kept"]), 4)               # 多出的 1 条当候补
        self.assertEqual(res["reasons"]["点赞不足"], 6)
        self.assertEqual(res["pool_size"], 10)

    def test_screen_pool_relaxes_when_insufficient(self):
        """严格档达标不足→自动降档，且降档轨迹可展示（未静默放宽）。"""
        from core.quality import screen_pool

        pool = ([{"video_id": "a", "url": "u_a", "like_count": 90000}]
                + [{"video_id": f"b{i}", "url": f"u_b{i}", "like_count": 800} for i in range(4)])
        res = screen_pool(pool, 4, level="strict")
        self.assertEqual(res["level"], "normal")
        self.assertEqual(res["relaxed"], ["strict→normal"])
        self.assertEqual(len(res["kept"]), 5)
        self.assertEqual(res["picked"][0]["url"], "u_a")     # 高赞的仍排第一

    def test_screen_pool_defers_without_likes(self):
        """搜索页解析不到点赞（选择器失效）：不猜数、不静默降标准，延后到详情页判定。"""
        from core.quality import screen_pool

        pool = [{"video_id": f"v{i}", "url": f"u{i}", "like_count": None} for i in range(8)]
        res = screen_pool(pool, 3)
        self.assertEqual(res["level"], "deferred")
        self.assertEqual(len(res["kept"]), 8)        # 候补队列给足，详情页逐条筛
        self.assertEqual(len(res["picked"]), 3)
        self.assertEqual(res["reasons"], {})

    def test_empty_pool_never_relaxes(self):
        """搜索 0 结果（撞验证码风控/关键词过冷）绝不能报成"素材不足已降档"。

        真实任务曾暴露过这个误导：风控导致候选池为空，却走完降档链报 loose，
        把排查方向从"撞风控"带偏到"门槛太严"。"""
        from core.quality import filter_note, screen_pool

        res = screen_pool([], 5)
        self.assertEqual(res["level"], "empty")
        self.assertEqual(res["relaxed"], [])          # 未发生降档
        self.assertEqual(res["kept"], [])
        self.assertEqual(res["picked"], [])
        note = filter_note(res, 5)
        self.assertIn("搜索无结果", note)
        self.assertIn("非门槛过严、未发生降档", note)
        self.assertNotIn("素材不足已降档", note)

    def test_gated_empty_candidates_explains_cause(self):
        """无候选可采时如实说明是搜索没结果，不报"候选池已用尽"。"""
        import crawler.tabs as ct

        orig_src = ct.base.require_ugc_source
        ct.base.require_ugc_source = lambda *a, **k: None
        try:
            res = ct.fetch_videos_gated(None, [], 5, level="strict")
        finally:
            ct.base.require_ugc_source = orig_src
        self.assertEqual(res["items"], [])
        self.assertEqual(res["fetched"], 0)
        self.assertFalse(res["exhausted"])            # 不是"用尽"，是根本没结果
        self.assertFalse(res["capped"])
        self.assertIn("搜索阶段未返回结果", res["note"])
        self.assertIn("不是门槛过严", res["note"])
        self.assertNotIn("候选池已用尽", res["note"])

    def test_gate_and_backfill_marks_reject(self):
        from core.quality import REJECT_LIKES, gate_and_backfill

        good = self._item(like=80000, vid="g", url="u_g")
        bad = self._item(like=50, vid="b", url="u_b")
        res = gate_and_backfill([{"url": "u_g"}, {"url": "u_b"}], [good, bad], 2, level="strict")
        self.assertEqual([i.video_id for i in res["kept"]], ["g"])
        self.assertEqual([i.video_id for i in res["rejected"]], ["b"])
        self.assertEqual(res["reasons"][REJECT_LIKES], 1)
        self.assertEqual(res["need_more"], 1)
        self.assertEqual(bad.quality_reject, REJECT_LIKES)   # 淘汰原因写回，供报告透明化
        self.assertEqual(good.quality_reject, "")
        self.assertIsNotNone(good.quality_score)

    def test_filter_note_is_transparent(self):
        """筛选明细必须说人话：候选池/达标数/候补/淘汰原因/降档轨迹/点赞区间一个不少。"""
        from core.quality import filter_note, screen_pool

        pool = ([{"video_id": "a", "url": "u_a", "like_count": 90000}]
                + [{"video_id": f"b{i}", "url": f"u_b{i}", "like_count": 800} for i in range(4)])
        note = filter_note(screen_pool(pool, 4, level="strict"), 4)
        for expect in ("候选池 5 条", "达标 4/4", "候补 1 条", "strict→normal",
                       "门槛档 normal", "未静默放宽", "点赞区间 800~90000"):
            self.assertIn(expect, note)
        # 淘汰原因也要看得见（不降档时）
        note2 = filter_note(screen_pool(pool, 1, level="strict", min_keep=1), 1)
        self.assertIn("点赞不足 4", note2)
        self.assertIn("门槛档 strict", note2)

    def test_fetch_videos_gated_backfills(self):
        """候补补采：首批不达标就从候补顶上，凑够即停（不多消耗导航额度）。"""
        import crawler.tabs as ct
        from core.models import VideoItem

        def fake_fetch(page, urls, **kw):
            return [(i, VideoItem(video_id=u, url=u, description="攻略", duration=120.0,
                                  like_count=90000 if "good" in u else 50,
                                  publish_time="2026-09-01"), None)
                    for i, u in enumerate(urls)]

        cands = [{"url": "u_bad1"}, {"url": "u_bad2"}, {"url": "u_good"}, {"url": "u_bad3"}]
        orig_fetch, orig_src = ct.fetch_videos, ct.base.require_ugc_source
        ct.fetch_videos = fake_fetch
        ct.base.require_ugc_source = lambda *a, **k: None
        try:
            res = ct.fetch_videos_gated(None, cands, 1, level="strict", max_fetch=4)
        finally:
            ct.fetch_videos, ct.base.require_ugc_source = orig_fetch, orig_src
        self.assertEqual([i.url for i in res["items"]], ["u_good"])
        self.assertEqual(res["fetched"], 3)      # 第1/2 条不达标 → 第3 条凑够即停
        self.assertEqual(res["reasons"]["点赞不足"], 2)
        self.assertFalse(res["capped"])
        self.assertEqual(res["relaxed"], [])     # 已凑够，不需降档

    def test_fetch_videos_gated_relaxes_without_extra_navigation(self):
        """素材不够时只对已采回的数据降档重判（零额外导航），并如实标注降档轨迹。"""
        import crawler.tabs as ct
        from core.models import VideoItem

        def fake_mid(page, urls, **kw):
            return [(i, VideoItem(video_id=u, url=u, description="攻略", like_count=800,
                                  publish_time="2026-09-01", duration=120.0), None)
                    for i, u in enumerate(urls)]

        orig_fetch, orig_src = ct.fetch_videos, ct.base.require_ugc_source
        ct.fetch_videos = fake_mid
        ct.base.require_ugc_source = lambda *a, **k: None
        try:
            res = ct.fetch_videos_gated(None, [{"url": "u_m1"}, {"url": "u_m2"}], 3,
                                        level="strict", max_fetch=2)
        finally:
            ct.fetch_videos, ct.base.require_ugc_source = orig_fetch, orig_src
        self.assertEqual(res["relaxed"], ["strict→normal"])
        self.assertEqual(len(res["items"]), 2)      # 降档后 800 赞达标，两条都留下
        self.assertEqual(res["fetched"], 2)         # 没有为凑数多导航一次
        self.assertIn("素材不足已降档", res["note"])
        self.assertIn("未静默放宽标准", res["note"])


class TestCrawlQualityWiring(unittest.TestCase):
    """M6-A 采集层接线：搜索地址排序参数 / 指标归一 / 排序向后兼容 / 多查询词扩池。"""

    def test_search_url_carries_sort_param(self):
        from crawler.douyin import search_url

        u = search_url("北京旅游攻略")
        self.assertIn("douyin.com/search/", u)
        self.assertIn("type=video", u)
        self.assertIn("sort_type=1", u)      # 最多点赞（平台不认也有本地质量闸兜底）

    def test_stat_normalizers(self):
        from crawler.douyin import _stat_duration, _stat_int

        self.assertEqual(_stat_int(1234), 1234)
        self.assertEqual(_stat_int("1.2万"), 12000)
        self.assertIsNone(_stat_int(None))
        self.assertIsNone(_stat_int(-1))
        self.assertEqual(_stat_duration(125000), 125.0)   # 毫秒 → 秒
        self.assertEqual(_stat_duration(45), 45.0)        # 已是秒
        self.assertIsNone(_stat_duration(0))
        self.assertIsNone(_stat_duration("abc"))

    def test_rank_candidates_backward_compatible(self):
        """排序改用质量分后，对只有点赞的候选结果与旧"点赞降序+无点赞垫后"一致。"""
        from crawler.douyin import rank_candidates

        cands = [
            {"video_id": "a", "url": "u_a", "like_count": 10},
            {"video_id": "b", "url": "u_b", "like_count": None},
            {"video_id": "a", "url": "u_dup", "like_count": 99},   # 重复 id 去重
            {"video_id": "c", "url": "u_c", "like_count": 50},
            {"video_id": "d", "url": "u_d", "like_count": 0},
            {"video_id": "e", "url": "u_e", "like_count": 30},
        ]
        self.assertEqual(rank_candidates(cands, 3), ["u_c", "u_e", "u_a"])
        self.assertEqual(rank_candidates(cands, 100), ["u_c", "u_e", "u_a", "u_b", "u_d"])
        self.assertEqual(rank_candidates([], 5), [])

    def test_search_pool_merges_queries(self):
        """多查询词合并候选池：按 video_id 去重、池满即停（风控成本优先）。"""
        from crawler import base
        from crawler.douyin import DouyinCrawler

        calls = []

        class FakeCrawler(DouyinCrawler):
            def __init__(self):        # 不碰浏览器
                pass

            def _search_one(self, keyword, max_n, scrolls=None):
                calls.append((keyword, max_n, scrolls))
                if keyword.endswith("攻略") and "避雷" not in keyword and "自由行" not in keyword:
                    return [{"video_id": f"h{i}", "url": f"u{i}", "like_count": 1000 + i}
                            for i in range(min(3, max_n))]      # 首轮只搜到 3 条
                return [{"video_id": "h0", "url": "u_dup", "like_count": 99999},   # 与首轮重复
                        {"video_id": "x1", "url": "u_x1", "like_count": 500}]

        orig_src = base.require_ugc_source
        base.require_ugc_source = lambda *a, **k: None
        try:
            fc = FakeCrawler()
            pool = fc.search_pool(["北京旅游攻略", "北京三天两夜"], pool_size=5)
            self.assertEqual([c["video_id"] for c in pool], ["h0", "h1", "h2", "x1"])  # h0 重复已去
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1], 5)      # 首轮索要满池
            self.assertEqual(calls[1][1], 2)      # 次轮只索要还差的数量（不浪费滚动）
            self.assertGreater(calls[0][2], calls[1][2])   # 补充角度滚动减半
            calls.clear()
            pool2 = fc.search_pool(["北京旅游攻略"], pool_size=3)
            self.assertEqual(len(calls), 1)       # 首轮就收满 → 不发多余导航
            self.assertEqual(len(pool2), 3)
        finally:
            base.require_ugc_source = orig_src

    def test_video_item_new_fields_roundtrip(self):
        """新质量字段随原始 JSON 落盘与还原；旧 JSON 无这些键时为 None（向后兼容）。"""
        import json

        from core.models import VideoItem
        from service.research import load_items_from_raw

        it = VideoItem(video_id="v1", url="u1", description="d", like_count=9000,
                       play_count=2000000, comment_count=800, collect_count=12000,
                       share_count=300, duration=125.0, publish_time="2026-09-01",
                       quality_score=0.8, quality_reject="")
        p = Path(tempfile.mkdtemp()) / "a.json"
        p.write_text(json.dumps([it.to_dict()], ensure_ascii=False), encoding="utf-8")
        back = load_items_from_raw(str(p))[0]
        self.assertEqual(back.play_count, 2000000)
        self.assertEqual(back.collect_count, 12000)
        self.assertEqual(back.share_count, 300)
        self.assertEqual(back.comment_count, 800)
        self.assertEqual(back.duration, 125.0)
        self.assertEqual(back.quality_score, 0.8)
        # 旧缓存（无新键）还原后均为 None，不会被质量闸误杀
        old = Path(tempfile.mkdtemp()) / "old.json"
        old.write_text(json.dumps([{"video_id": "v2", "url": "u2", "like_count": 10}]),
                       encoding="utf-8")
        o = load_items_from_raw(str(old))[0]
        self.assertIsNone(o.play_count)
        self.assertIsNone(o.collect_count)
        self.assertIsNone(o.duration)
        self.assertEqual(o.quality_reject, "")


class TestCityGuideLayer(unittest.TestCase):
    """M6-B 城市攻略层：查询矩阵 / 提炼归一 / 圈定与排线接线 / 缓存往返 / 渲染透明化。"""

    def test_guide_queries_matrix(self):
        """用户要去某地旅游，搜的应该是"{城市}旅游攻略""{城市}三天两夜"，不是单个景点名。"""
        from service.trip import _city_guide_queries

        self.assertEqual(_city_guide_queries("北京", 3),
                         ["北京旅游攻略", "北京三天两夜", "北京旅游避雷", "北京自由行攻略"])
        self.assertEqual(_city_guide_queries("大同", 2)[1], "大同两天一夜")
        self.assertEqual(_city_guide_queries("上海", 1)[1], "上海一天一夜")
        self.assertEqual(_city_guide_queries("西安", 9)[1], "西安9天8夜")   # 超出中文表回退数字

    def test_normalize_mention_defaults(self):
        from pipeline.candidates import _normalize_mention

        self.assertEqual(_normalize_mention({"name": "云冈石窟", "category": "景点", "heat": "高"}),
                         {"name": "云冈石窟", "category": "景点", "heat": "高", "note": ""})
        self.assertIsNone(_normalize_mention({"name": "  "}))
        self.assertIsNone(_normalize_mention("不是字典"))
        # 类别/热度越界回默认（防 LLM 自由发挥）
        self.assertEqual(_normalize_mention({"name": "甲", "category": "网红打卡"})["category"], "景点")
        self.assertEqual(_normalize_mention({"name": "甲", "heat": "爆"})["heat"], "中")

    def test_guide_corpus_and_empty_guard(self):
        from core.models import Comment, VideoItem
        from pipeline.candidates import _guide_corpus, empty_guide_knowledge, extract_guide_knowledge

        it = VideoItem(video_id="v1", url="https://x/1", description="北京三天两夜攻略",
                       like_count=80000, collect_count=12000, publish_time="2026-08-01",
                       comments=[Comment(text="故宫要提前预约", like_count=900),
                                 Comment(text="住前门方便", like_count=300)])
        corpus = _guide_corpus([it])
        for expect in ("点赞 80000", "收藏 12000", "北京三天两夜攻略",
                       "故宫要提前预约", "https://x/1"):
            self.assertIn(expect, corpus)
        # 素材为空：直接给空骨架，一次 LLM 都不调
        self.assertEqual(extract_guide_knowledge([]), empty_guide_knowledge())
        self.assertEqual(empty_guide_knowledge()["guide_candidates"], [])

    def test_extract_guide_knowledge_normalizes(self):
        import pipeline.candidates as pc
        from core.models import VideoItem

        it = VideoItem(video_id="v1", url="https://x/1", description="攻略", like_count=1000)
        orig = pc.chat_json
        pc.chat_json = lambda *a, **k: {
            "mentions": [{"name": "故宫", "category": "景点", "heat": "高", "note": "提前预约"},
                         {"name": "故宫", "category": "景点", "heat": "高"},      # 重复要去掉
                         {"name": "", "category": "景点"},                        # 无名丢弃
                         {"name": "南锣鼓巷", "category": "瞎写", "heat": "爆"}],  # 越界回默认
            "plan_hints": ["故宫和景山连着走", "  ", "故宫和景山连着走", "周一多数博物馆闭馆"],
            "itineraries": [
                {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "故宫"}]}]},
                {"days": [{"day": "坏", "slots": [{"spot": "X"}]}]},   # 非法天丢弃
                "垃圾"],                                                      # 非对象丢弃
            "days_advice": 99,          # 超区间视为无效
            "stay_advice": "前门/王府井",
        }
        try:
            g = pc.extract_guide_knowledge([it], city="北京", days=2)
        finally:
            pc.chat_json = orig
        self.assertEqual([m["name"] for m in g["guide_candidates"]], ["故宫", "南锣鼓巷"])
        self.assertEqual(g["guide_candidates"][1]["category"], "景点")   # 越界回默认
        self.assertEqual(g["guide_candidates"][1]["heat"], "中")
        self.assertEqual(g["plan_hints"], ["故宫和景山连着走", "周一多数博物馆闭馆"])  # 去空去重
        self.assertIsNone(g["days_advice"])       # 99 天不合法，不拿它覆盖用户输入
        self.assertEqual(g["stay_advice"], "前门/王府井")
        self.assertEqual(g["videos"], 1)
        self.assertEqual(g["sources"], ["https://x/1"])
        # 视频行程草案一并提炼（M6-C）；非法项不污染草案
        self.assertEqual(g["guide_itineraries"],
                         [{"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "故宫"}]}]}])

    def test_extract_guide_knowledge_survives_llm_failure(self):
        """提炼失败一律给空骨架：攻略层是增强项，绝不阻断行程生成。"""
        import pipeline.candidates as pc
        from core.models import VideoItem

        it = VideoItem(video_id="v1", url="u", description="攻略", like_count=1000)
        orig = pc.chat_json

        def boom(*a, **k):
            raise RuntimeError("no llm")

        pc.chat_json = boom
        try:
            g = pc.extract_guide_knowledge([it], city="北京", days=2)
        finally:
            pc.chat_json = orig
        self.assertEqual(g, pc.empty_guide_knowledge())

    def test_generate_candidates_uses_guide_evidence(self):
        """有攻略实证时候选以真实提到的点为底稿（提示词必须带上），不再凭空想象。"""
        import pipeline.candidates as pc

        seen = {}
        orig = pc.chat_json

        def fake(system, user, **kw):
            seen["user"] = user
            return {"candidates": [{"name": "故宫", "category": "景点", "reason": "攻略高频"}]}

        pc.chat_json = fake
        try:
            pc.generate_candidates("北京", 2, "", guide_evidence={
                "guide_candidates": [{"name": "故宫", "category": "景点", "heat": "高",
                                      "note": "提前预约"}],
                "plan_hints": ["故宫和景山连着走"], "stay_advice": "前门"})
            with_ev = seen["user"]
            pc.generate_candidates("北京", 2, "")
            without_ev = seen["user"]
        finally:
            pc.chat_json = orig
        for expect in ("故宫", "攻略提及热度高", "提前预约", "故宫和景山连着走", "前门"):
            self.assertIn(expect, with_ev)
        self.assertNotIn("攻略提及热度", without_ev)   # 不传实证 = 原纯 LLM 行为（零回归）

    def test_plan_itinerary_eats_guide_hints(self):
        """排线吃攻略编排知识：提示词里必须出现真实攻略建议。"""
        import pipeline.planner as pp

        seen = {}
        orig = pp.chat_json

        def fake(system, user, **kw):
            seen["user"] = user
            return {"days": [{"slots": [{"slot": "上午", "spot": "故宫"}]}]}

        pp.chat_json = fake
        profiles = {"故宫": {"duration_hours": 3, "best_time_slot": "上午", "highlights": [],
                          "avoid": [], "food": [], "photo_spots": [], "tips": [],
                          "cost_items": []}}
        try:
            pp.plan_itinerary("北京", 1, "", profiles, [], "",
                              guide_hints=["故宫和景山连着走"])
            with_hints = seen["user"]
            pp.plan_itinerary("北京", 1, "", profiles, [], "")
            without = seen["user"]
        finally:
            pp.chat_json = orig
        self.assertIn("真实攻略的编排建议", with_hints)
        self.assertIn("故宫和景山连着走", with_hints)
        self.assertNotIn("真实攻略的编排建议", without)   # 不传则零回归

    def test_knowledge_guide_roundtrip(self):
        """攻略缓存往返：按城市精确命中、提炼结果一并复用、空采集不算命中。"""
        from core import knowledge

        tmp_db = Path(tempfile.mkdtemp()) / "test_guide.db"
        raw_dir = Path(tempfile.mkdtemp())
        orig = knowledge._DB_PATH
        knowledge._DB_PATH = tmp_db
        try:
            f = raw_dir / "guide.json"
            f.write_text("[]", encoding="utf-8")
            guide = {"guide_candidates": [{"name": "故宫", "category": "景点", "heat": "高",
                                          "note": ""}],
                     "plan_hints": ["故宫和景山连着走"], "days_advice": 3,
                     "stay_advice": "前门", "sources": ["u1"], "videos": 6}
            knowledge.record_guide("北京", str(f), 6, guide)
            hit = knowledge.find_guide("北京", 14)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["video_count"], 6)
            self.assertEqual(hit["guide"]["plan_hints"], ["故宫和景山连着走"])
            self.assertIsNone(knowledge.find_guide("上海", 14))       # 别的城市不串
            # 关键：不得误命中同城市的普通景点缓存（find_fresh 的前缀回退有这个坑）
            knowledge.record_crawl("北京", str(f), 5, 50)
            self.assertEqual(knowledge.find_guide("北京", 14)["video_count"], 6)
            # 空采集不算命中（缓存投毒防护）
            knowledge.record_guide("天津", str(f), 0, guide)
            self.assertIsNone(knowledge.find_guide("天津", 14))
        finally:
            knowledge._DB_PATH = orig

    def test_guide_note_and_render(self):
        """攻略层来源说明进报告概览（用户看得见实证依据从哪来）；无产物则不输出该行。"""
        from pipeline.decision import build_decisions, build_trip_plan
        from pipeline.trip_render import render_markdown
        from service.trip import _guide_note

        note = _guide_note({"videos": 6,
                            "guide_candidates": [{"name": f"点{i}"} for i in range(8)],
                            "plan_hints": ["a", "b"], "stay_advice": "前门"})
        for expect in ("城市攻略层 6 条高赞综合攻略视频", "实证候选 8 个", "点0", "…",
                       "编排建议 2 条", "推荐住宿片区 前门"):
            self.assertIn(expect, note)
        self.assertEqual(_guide_note({}), "")
        self.assertEqual(_guide_note({"guide_candidates": [], "plan_hints": []}), "")

        plan = {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "故宫"}]}]}
        profiles = {"故宫": {"duration_hours": 3, "best_time_slot": "上午", "highlights": [],
                          "avoid": [], "food": [], "photo_spots": [], "tips": [],
                          "cost_items": []}}
        decs = build_decisions(profiles=profiles, food_profiles={}, heat_rows=[],
                               official_facts={}, plan=plan)
        tp = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan,
                             snap={"guide_note": note, "profiles": profiles,
                                   "foods": {}, "heat": []}).to_dict()
        self.assertIn(f"📚 {note}", render_markdown(tp))
        # 不传 guide_note：概览里不该冒出这一行（零回归）
        tp2 = build_trip_plan(meta={"city": "北京", "days": 1}, decisions=decs, plan=plan,
                              snap={"profiles": profiles, "foods": {}, "heat": []}).to_dict()
        self.assertNotIn("📚", render_markdown(tp2))


class TestM6cDraftChain(unittest.TestCase):
    """M6-C：视频行程草案链路——提炼草案 → LLM 审核增删改 → 候选前置 → 验证优先 → 规划注入。"""

    _GUIDE = {
        "guide_candidates": [{"name": "故宫", "category": "景点", "heat": "高", "note": ""}],
        "guide_itineraries": [{"days": [
            {"day": 1, "slots": [{"slot": "上午", "spot": "故宫"},
                                    {"slot": "下午", "spot": "景山公园"}]},
            {"day": 2, "slots": [{"slot": "全天", "spot": "环球影城"}]}]}],
    }

    def test_normalize_itinerary_and_names(self):
        """单条草案归一：时段越界回空、非法天丢弃、items/period/name 别名兼容；点名去重保序。"""
        import pipeline.candidates as pc

        d = pc._normalize_itinerary({"days": [
            {"day": 1, "slots": [{"slot": "上午", "spot": "故宫"},
                                    {"slot": "瞎写", "spot": "景山公园"},     # 时段越界→空
                                    {"slot": "下午", "spot": "  "}]},        # 无点名丢弃
            {"day": "二", "slots": [{"spot": "X"}]},                       # 天数非法丢弃
            {"day": 2, "items": [{"period": "全天", "name": "环球影城"}]},  # items/period/name 别名
        ]})
        self.assertEqual([x["day"] for x in d["days"]], [1, 2])
        self.assertEqual(d["days"][0]["slots"],
                         [{"slot": "上午", "spot": "故宫"}, {"slot": "", "spot": "景山公园"}])
        self.assertEqual(d["days"][1]["slots"], [{"slot": "全天", "spot": "环球影城"}])
        # 无有效天/非法入参 → None（不拿半截草案污染审核输入）
        self.assertIsNone(pc._normalize_itinerary({"days": []}))
        self.assertIsNone(pc._normalize_itinerary(None))
        # 点位名去重保序（验证优先级与候选注入共用）
        names = pc.draft_spot_names({"days": [
            {"day": 1, "slots": [{"spot": "故宫"}, {"spot": "景山公园"}]},
            {"day": 2, "slots": [{"spot": "故宫"}, {"spot": "环球影城"}]}]})
        self.assertEqual(names, ["故宫", "景山公园", "环球影城"])
        self.assertEqual(pc.draft_spot_names(None), [])

    def test_review_guide_itinerary_merges_and_degrades(self):
        """审核：合并多条视频编排→天数对齐（超出截断）→notes 回传；无素材/LLM 失败一律空。"""
        import pipeline.candidates as pc

        seen = {}
        orig = pc.chat_json
        pc.chat_json = lambda system, user, **k: (
            seen.update(user=user),
            {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "故宫"},
                                               {"slot": "下午", "spot": "景山公园"}]},
                       {"day": 2, "slots": [{"slot": "全天", "spot": "环球影城"}]},
                       {"day": 3, "slots": [{"slot": "上午", "spot": "越界天"}]}],
             "notes": "砍了第 3 天，主干按多视频共识保留"})[1]
        try:
            r = pc.review_guide_itinerary("北京", 2, "亲子", self._GUIDE)
        finally:
            pc.chat_json = orig
        self.assertIn("视频行程草案", seen["user"])          # 草案原文喂进审核
        self.assertIn("上午 故宫", seen["user"])
        self.assertIn("候选点池", seen["user"])              # 可挑点补足天数
        self.assertEqual([d["day"] for d in r["days"]], [1, 2])   # 超出用户天数被截断
        self.assertEqual(r["days"][1]["slots"], [{"slot": "全天", "spot": "环球影城"}])
        self.assertEqual(r["notes"], "砍了第 3 天，主干按多视频共识保留")
        # 无草案素材 → {}（调用方跳过注草案，零回归）
        self.assertEqual(pc.review_guide_itinerary("北京", 2, "", {}), {})
        # 审核 LLM 失败 → {}（增强项不阻断主流程）
        def boom(*a, **k):
            raise RuntimeError("no llm")

        pc.chat_json = boom
        try:
            self.assertEqual(pc.review_guide_itinerary("北京", 2, "", self._GUIDE), {})
        finally:
            pc.chat_json = orig

    def test_generate_candidates_draft_priority(self):
        """草案点位确定性前置（LLM 没返回也在清单里）、去重，且提示词点名要求全纳入。"""
        import pipeline.candidates as pc

        seen = {}
        orig = pc.chat_json
        pc.chat_json = lambda system, user, **k: (
            seen.update(user=user),
            {"candidates": [{"name": "环球影城", "category": "景点", "reason": "llm"},
                            {"name": "故宫", "category": "景点", "reason": "llm"}]})[1]
        draft = {"days": [{"day": 1, "slots": [
            {"slot": "上午", "spot": "故宫"}, {"slot": "下午", "spot": "国博"}]}]}
        try:
            cands = pc.generate_candidates("北京", 1, "", draft_plan=draft)
        finally:
            pc.chat_json = orig
        names = [c["name"] for c in cands]
        self.assertEqual(names[:2], ["故宫", "国博"])       # 草案点位前置且保序
        self.assertIn("环球影城", names)                    # LLM 补充点不丢
        self.assertEqual(names.count("故宫"), 1)            # 草案与 LLM 重叠去重
        self.assertIn("必须全部纳入候选清单", seen["user"])
        self.assertIn("国博", seen["user"])

    def test_select_verify_priority_first(self):
        """验证名额：草案点位先占，剩余按类别配额公平分配；幽灵名不占名额。"""
        import pipeline.candidates as pc

        cands = [{"name": n, "category": "景点"} for n in ("甲", "乙", "丙", "丁")] + \
                [{"name": "食A", "category": "美食"}, {"name": "食B", "category": "美食"}]
        picked = [c["name"] for c in pc.select_verify_candidates(
            cands, verify_max=4, priority=["丁", "食B"])]
        self.assertEqual(picked, ["丁", "食B", "甲", "乙"])   # 草案优先，其余走配额桶
        fallback = [c["name"] for c in pc.select_verify_candidates(
            cands, verify_max=4, priority=["不存在"])]
        self.assertEqual(len(fallback), 4)                     # 不在候选清单的名字被忽略
        self.assertNotIn("不存在", fallback)

    def test_format_and_inject_draft_plan(self):
        """format_draft_plan 文本化 + plan_itinerary 提示词注入（不传则零回归）。"""
        import pipeline.planner as pp
        from pipeline.planner import format_draft_plan

        draft = {"days": [{"day": 1, "slots": [{"slot": "上午", "spot": "故宫"},
                                                {"slot": "下午", "spot": "景山公园"}]}],
                 "notes": "补了景山公园"}
        txt = format_draft_plan(draft)
        self.assertIn("第 1 天：上午 故宫；下午 景山公园", txt)
        self.assertIn("（审核说明：补了景山公园）", txt)
        self.assertEqual(format_draft_plan(None), "")
        self.assertEqual(format_draft_plan({"days": []}), "")

        seen = {}
        orig = pp.chat_json

        def fake(system, user, **k):
            seen["user"] = user
            return {"days": [{"slots": [{"slot": "上午", "spot": "故宫"}]}]}

        profiles = {"故宫": {"duration_hours": 3, "best_time_slot": "上午", "highlights": [],
                          "avoid": [], "food": [], "photo_spots": [], "tips": [],
                          "cost_items": []}}
        pp.chat_json = fake
        try:
            pp.plan_itinerary("北京", 1, "", profiles, [], "", draft_plan=draft)
            with_draft = seen["user"]
            pp.plan_itinerary("北京", 1, "", profiles, [], "")
            without = seen["user"]
        finally:
            pp.chat_json = orig
        self.assertIn("高赞攻略视频的行程草案", with_draft)   # 主干优先采用的指令
        self.assertIn("景山公园", with_draft)
        self.assertNotIn("高赞攻略视频的行程草案", without)   # 不传草案 = 零回归


if __name__ == "__main__":
    unittest.main(verbosity=2)
