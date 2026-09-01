# TravelGuide Assistant API 参考

基础地址：`http://127.0.0.1:8000`（可用环境变量 `SERVER_HOST` / `SERVER_PORT` 覆盖）。

## GET /api/health

健康检查 + 知识库概览。

```json
{"status": "ok", "kb": {"spots": 3, "reports": 2, "latest_crawl": "2026-08-30T14:00:55"}, "kb_ttl_days": 7}
```

## POST /api/research

发起研究任务。

请求体：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `keyword` | string | 必填 | 景点关键词，自动归一化（"西湖攻略"≈"西湖"） |
| `mode` | string | `standard` | `fast`（5 视频）/ `standard`（8 视频）/ `deep`（10 视频 + 口播转写） |
| `force` | bool | false | true 跳过知识库缓存 |

响应：`{"job_id": "a1b2c3d4e5f6"}`

## GET /api/jobs/{job_id}

任务状态。顶层字段：

| 字段 | 说明 |
|---|---|
| `status` | `running` / `done` / `error` / `cancelled` |
| `stage` | 当前阶段：排队中/查询知识库/采集数据/提取要点/补全缺口/交叉验证/生成报告/完成/失败/已取消 |
| `log` | 带时间戳的日志数组（轮询时取末尾 3~5 行展示即可） |
| `cache_hit` | 是否命中知识库缓存 |
| `result` | 仅 `done` 时有，见下 |
| `error` | 仅 `error` 时有 |

`result` 结构：

```json
{
  "report_path": "C:/.../data/reports/西湖_20260901_120000.md",
  "report_name": "西湖_20260901_120000.md",
  "markdown": "# 《西湖》旅游攻略报告 ...",
  "cache_hit": false,
  "stats": {"videos": 8, "comments": 320, "points": 42, "elapsed": 361.2}
}
```

服务重启后历史任务仍可查询（终态已持久化），但 `log` 为空。

## POST /api/jobs/{job_id}/cancel

取消运行中的任务。成功 `{"ok": true}`；任务不存在 404；非运行态 409。
取消在当前步骤结束后生效，浏览器等资源自动释放。

## GET /api/jobs/history

最近终态任务摘要：`{"jobs": [{"id", "keyword", "mode", "status", "error", "finished_at"}, ...]}`

## GET /api/reports

历史报告列表（文件仍存在的）：

```json
{"reports": [{"keyword", "video_count", "comment_count", "crawled_at", "reported_at", "report_path"}, ...]}
```

注意：`report_path` 此处是**文件名**（非完整路径）。

## GET /api/reports/download?name=文件名

下载指定报告（`text/markdown`）。仅接受 `data/reports/` 内的 `.md` 文件，路径穿越返回 404。

## 典型耗时参考

| 环节 | 耗时 |
|---|---|
| 缓存命中全流程 | 约 30 秒 |
| 采集（浏览器，频控约束） | 10 视频 ≈ 5 分钟 |
| 提取（3 路并发） | 约 40 秒 |
| 缺口补全（触发时） | +1~3 分钟 |
| 交叉验证 | 约 10 秒（开思考模式 3~4 分钟） |
| 报告生成 | 约 20 秒 |
| deep 模式口播转写 | +8~15 分钟（CPU） |
