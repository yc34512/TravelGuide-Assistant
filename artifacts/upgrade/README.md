# 升级验收机器结果

这些文件由 `python scripts/verify_all.py` 生成（`s0_counterexamples.json` 由 S0
反例脚本单独生成）。它们只对应当前机器与当前的离线/本地替身配置：

- `test_run.log`：完整离线测试套件输出；
- `agent_eval.json`：脚本模型 + 合成夹具的工具 Agent 对照；`real_model=false`；
- `fault_injection.json`：故障注入与既有恢复测试；`real_services=false`；
- `capacity_report.json`：本地 SQLite + mock handler 容量；`not_online_qps=true`；
- `migration_dry_run.json`：只读盘点，没有写入 MySQL；
- `ui_logic.log`：从 `web/index.html` 抽取的版本函数在模拟 DOM/fetch 下的检查；
- `s0_counterexamples.json`：采集状态与 FAISS 分数映射的反例/预期；
- `verify_summary.json`：一键验收的通过/失败汇总。

未验收项：真实 MySQL/Redis 服务器、真实付费模型、真实浏览器点击流程、真实 faiss。
不要把这里的 mock/脚本结果写成线上 QPS、准确率或 token 节省。