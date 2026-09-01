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

## POST /api/trip

发起行程规划任务（混合候选验证 → 逐点调研 → 预算控制 → 排线）。

请求体：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `city` | string | 必填 | 目的地城市 |
| `days` | int | 2 | 出行天数 1~7 |
| `hotel` | string | "" | 酒店/住宿位置（用于排线） |
| `spots` | string[] | null | 指定景点清单；缺省时自动圈定 15~20 个候选并经抖音验证筛选 |
| `preferences` | string | "" | 用户偏好（如"带老人"） |
| `budget` | number | null | 总预算（元，不含大交通），传入后输出预算明细与超支预警 |
| `preference_mode` | string | 均衡 | 省钱优先 / 体验优先 / 均衡 |

响应：`{"job_id": "..."}`；轮询同 `/api/jobs/{job_id}`（`stage` 依次为：圈定景点/调研景点/构建档案/计算路线/生成规划/渲染路书）。
行程任务的 `result` 额外含：`budget_summary`（门票/餐饮/交通/弹性分项与结余/超支判定）、
`pitfall_digest`（避坑专题，每条含评论原文 `quote` 与来源 `source`）、`heat_rank`（热度榜）、
`html_name`（HTML 可视化报告文件名，用下载接口取）。
耗时：未调研过的景点约 5 分钟/个，调研过的秒级命中缓存。
行程任务的景点详情卡内嵌"真实评价摘要"（总评/好评/差评/评论摘录）。

## POST /api/heat/refresh

发起城市热度刷榜任务（后台）。请求体：`{"city": "大同"}`，响应：`{"job_id": "..."}`。
对最多 6 个热门景点做元数据轻量采集（只取点赞与发布时间，不采评论），约 3~5 分钟；
用 `GET /api/jobs/{job_id}` 轮询，完成后榜单落库。

## GET /api/heat/{city}

查询城市实时热度榜（读库，秒回）：

```json
{"city": "大同", "ranking": [{"spot", "score", "trend": "本周最火|正在降温|平稳",
  "fresh7", "likes", "videos", "updated_at"}], "hint": "空榜时的引导文案"}
```

趋势判定（四态）：近 7 天新视频占比 ≥ 30% 为"本周最火"；综合分 ≥ 0.6 但新增不多为"长盛不衰"；
60 天以上旧内容占比 ≥ 50% 且近 7 天几乎无新增且热度不高为"正在降温"；其余"平稳"。

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

下载指定报告（`text/markdown` 或 `text/html`）。仅接受 `data/reports/` 内的 `.md` / `.html` 文件，路径穿越返回 404。

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
