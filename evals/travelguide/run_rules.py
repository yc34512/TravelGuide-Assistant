"""固定策略反例评测，不调用模型。手工合成的QC状态不可当作旅游准确率。

运行：python evals/travelguide/run_rules.py --output evals/travelguide/results.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.planning_runs import fingerprint
from pipeline.planning_policy import prefer

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
fixture_path = Path(__file__).with_name("policy_cases.json")
fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
rows = []
for case in fixtures["cases"]:
    actual = {policy: {"adopt": decision[0], "reason": decision[1]}
              for policy in ("legacy-v1", "hard-first-v1")
              for decision in [prefer(case["old"], case["new"], policy)]}
    passed = all(actual[p]["adopt"] == expect for p, expect in case["expected"].items())
    rows.append({"id": case["id"], "label_source": case["label_source"],
                 "input_hash": fingerprint({"old": case["old"], "new": case["new"]}),
                 "expected": case["expected"], "actual": actual, "passed": passed})
output = {"evaluation": "synthetic_policy_counterexamples", "fixture_version": fixtures["version"],
          "source_hash": fingerprint(fixtures), "model": None, "model_requests": 0,
          "token_usage": None, "correct_cases": sum(r["passed"] for r in rows),
          "total_cases": len(rows), "cases": rows,
          "scope": "规则采纳反例；不代表真实模型效果、旅游事实准确率或成本节省"}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Policy counterexamples: {output['correct_cases']}/{output['total_cases']}; model_requests=0")
sys.exit(0 if output["correct_cases"] == output["total_cases"] else 1)
