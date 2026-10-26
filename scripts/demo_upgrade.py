"""不联网的升级演示种子：生成两份合成行程版本，供页面/API 验收。

    python scripts/demo_upgrade.py            # 只生成数据
    python scripts/demo_upgrade.py --serve    # 生成后在 8123 启动本地服务

所有城市、点位、价格均为合成夹具，不代表真实旅游信息。
数据写入 data/demo_upgrade/，不触碰正式知识库与报告目录。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "demo_upgrade"
os.environ.setdefault("TASK_DB_PATH", str(DEMO / "tasks.db"))
os.environ.setdefault("REPORT_DB_PATH", str(DEMO / "reports.db"))
os.environ.setdefault("KNOWLEDGE_DB_PATH", str(DEMO / "knowledge.db"))
os.environ.setdefault("REPORT_DIR", str(DEMO / "reports"))
os.environ.setdefault("RAW_DIR", str(DEMO / "raw"))
os.environ.setdefault("DEBUG_DIR", str(DEMO / "debug"))
os.environ.setdefault("WORKER_ENABLED", "false")
os.environ.setdefault("SOURCE_DOUYIN_ENABLED", "false")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import knowledge  # noqa: E402
from core.storage.reports import SQLiteReportStore  # noqa: E402
from core.storage.tasks import SQLiteTaskStore  # noqa: E402
from config import (KNOWLEDGE_DB_PATH, LOCAL_OWNER_ID, REPORT_DB_PATH,  # noqa: E402
                    REPORT_DIR, TASK_DB_PATH)
from service.task_service import ensure_version_for_job  # noqa: E402


def _result(spot_a, spot_b, cutoff="2026-10-01"):
    name = f"行程_演示城_1天_{spot_a}_{spot_b}.md".replace("/", "_")
    markdown = f"""# 演示城 1 日行程

> 合成演示数据，不是真实旅游建议。

## 第 1 天

- 上午：{spot_a}
- 下午：{spot_b}

## 需要出发前确认

- 票价与开放时间均为演示值，请以官方为准。
"""
    return {
        "report_name": name, "markdown": markdown,
        "quality": {"passed": True, "score": 96, "unresolved": []},
        "trip_plan": {
            "meta": {"city": "演示城", "days": 1, "stay": "演示酒店",
                     "prefs": "合成夹具", "preference_mode": "均衡",
                     "generated_at": f"{cutoff}T10:00:00", "facts_cutoff": cutoff,
                     "start_date": cutoff, "planning_policy": "hard-first-v1"},
            "itinerary": [{"day": 1, "blocks": [
                {"slot": "上午", "spot": spot_a, "transport": "公交约 20 分钟"},
                {"slot": "下午", "spot": spot_b, "transport": "步行约 15 分钟"},
            ]}],
            "catalog": {"poi": {
                spot_a: {"ticket_price": 60, "open_hours": "09:00-17:00",
                         "close_day": "", "official": {"source_url": "https://official.test/a"}},
                spot_b: {"ticket_price": 120, "open_hours": "10:00-18:00",
                         "close_day": "", "official": {"source_url": "https://official.test/b"}},
            }, "food": {}},
            "quality": {"passed": True, "score": 96, "unresolved": [],
                        "constraint_coverage": {"hard_ids": [], "issues": []}},
            "snap": {
                "evidence_version": 1,
                "profiles": {spot_a: {"highlights": [f"{spot_a}需要提前预约"], "avoid": []},
                             spot_b: {"highlights": [f"{spot_b}门票较贵"], "avoid": []}},
                "pitfall": [{"claim": f"{spot_a}需要预约", "quote": f"{spot_a}必须提前预约",
                             "source": "https://ugc.test/demo1", "support_status": "supported",
                             "evidence_id": "demo-ev-1", "confidence": "多源一致"}],
                "digests": {spot_b: {
                    "verdict": "票价偏高但展陈完整",
                    "quote_ids": ["demo-q2"],
                    "quote_points": [{"quote_id": "demo-q2", "evidence_id": "demo-ev-2",
                                      "quote": f"{spot_b}门票 120 元",
                                      "source": "https://ugc.test/demo2",
                                      "support_status": "supported"}],
                }},
            },
        },
    }


def seed() -> dict:
    DEMO.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    task_store = SQLiteTaskStore(TASK_DB_PATH)
    reports = SQLiteReportStore(REPORT_DB_PATH)
    payload = {"city": "演示城", "days": 1, "hotel": "演示酒店", "spots": None,
               "preferences": "合成夹具", "preference_mode": "均衡",
               "start_date": "2026-10-01", "excluded_spots": []}
    out = task_store.submit(owner_id=LOCAL_OWNER_ID, idempotency_key="demo-v1",
                            kind="trip", queue_name="pipeline", payload=payload)
    jid = out["id"]
    current = task_store.get(jid)
    result = _result("甲园", "乙馆")
    if current["state"] != "succeeded":
        job = task_store.claim("pipeline", "demo-seed", 30)
        if not job or job["id"] != jid:
            raise RuntimeError("演示任务领取异常")
        task_store.complete(jid, "demo-seed", job["generation"], state="succeeded",
                            result=result, quality_status="pass",
                            result_ref=str(REPORT_DIR / result["report_name"]))
    version1 = ensure_version_for_job(task_store.get(jid))
    if version1 is None:
        raise RuntimeError("演示版本登记失败")
    report_path = REPORT_DIR / result["report_name"]
    report_path.write_text(result["markdown"], encoding="utf-8")
    knowledge.register_report("行程·演示城1天", str(report_path), 12, 40)

    result2 = _result("甲园", "丙塔")
    payload2 = {**payload, "excluded_spots": ["乙馆"],
                "preferences": "合成夹具；变更：第二天改室内"}
    version2 = reports.register_result(
        job_id="demo-v2", owner_id=LOCAL_OWNER_ID, result=result2,
        report_id=version1["report_id"], parent_version_id=version1["version_id"],
        change_summary="排除乙馆，改为丙塔", activate=False, request=payload2)
    (REPORT_DIR / result2["report_name"]).write_text(result2["markdown"], encoding="utf-8")
    knowledge.register_report("行程·演示城1天（变更）", str(REPORT_DIR / result2["report_name"]),
                              12, 40)
    state = {
        "job_id": jid, "report_id": version1["report_id"],
        "version1": version1["version_id"], "version2": version2["version_id"],
        "report_name": result["report_name"],
        "url": "http://127.0.0.1:8123/",
        "note": "合成演示数据；不联网",
    }
    (DEMO / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="不联网升级演示")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args(argv)
    state = seed()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if not args.serve:
        return 0
    env = dict(os.environ)
    env["SERVER_PORT"] = str(args.port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api_server:app", "--host", "127.0.0.1",
         "--port", str(args.port), "--log-level", "warning"],
        cwd=ROOT, env=env)
    print(f"demo server: http://127.0.0.1:{args.port}")
    try:
        while proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        proc.terminate()
    return proc.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())