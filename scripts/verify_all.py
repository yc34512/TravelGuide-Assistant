"""一键复现：离线测试 + Agent 对照 + 故障注入 + 本地容量 + 迁移 dry-run。

不调用真实模型、不访问平台、不启动 MySQL/Redis。结果统一落到 artifacts/upgrade/。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "artifacts" / "upgrade"
DATA = ROOT / "data" / "upgrade"


def run(name, cmd, timeout=900):
    print(f"[verify] {name}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout)
    text = proc.stdout + proc.stderr
    (OUT / f"{name}.log").write_text(text, encoding="utf-8")
    print(f"[verify] {name}: exit={proc.returncode}")
    return proc.returncode, text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TravelGuide 升级一键验收")
    parser.add_argument("--skip-capacity", action="store_true")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    code, text = run("test_run", [sys.executable, "tests_offline.py"])
    results["tests"] = {"ok": code == 0, "tail": text.strip().splitlines()[-1:]}
    code, _ = run("eval_agent", [sys.executable, "scripts/eval_agent.py"])
    results["agent_eval"] = {"ok": code == 0}
    code, _ = run("fault_injection", [sys.executable, "scripts/fault_injection.py"])
    results["fault_injection"] = {"ok": code == 0}
    code, _ = run("migration_dry_run", [sys.executable,
                                       "scripts/migrate_sqlite_to_mysql.py"])
    results["migration_dry_run"] = {"ok": code == 0}
    node = shutil.which("node")
    if node:
        code, _ = run("ui_logic", [node, "scripts/ui_logic_check.js"])
        results["ui_logic"] = {"ok": code == 0}
    else:
        results["ui_logic"] = {"ok": True, "skipped": "node not installed"}
    if not args.skip_capacity:
        code, _ = run("capacity", [sys.executable, "scripts/capacity_test.py"], timeout=600)
        results["capacity"] = {"ok": code == 0}
    for name in ("agent_eval.json", "fault_injection.json", "capacity_report.json",
                 "migration_dry_run.json"):
        src = DATA / name
        if src.exists():
            shutil.copy2(src, OUT / name)
    (OUT / "verify_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = all(v.get("ok") for v in results.values())
    print("[verify] all ok" if ok else "[verify] FAILURES — see artifacts/upgrade/")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())