# TravelGuide Assistant（旅游攻略助手）

输入一个景点关键词 → 自动采集抖音上相关视频的文案与高赞评论 → AI 交叉提炼碎片化攻略信息 → 生成一份**带来源引用**的旅游攻略报告（Markdown）。

第一阶段竖切：**仅抖音**。小红书接入预留了适配器接口，二期再加。

## 合规声明（请务必阅读）

- 采用**浏览器自动化**路线（DrissionPage）：以真实浏览器、普通用户身份访问，只读取登录后页面可见的内容；**不破解签名、不绕过风控、不碰登录墙后的非公开数据**。
- 评论数据**去标识化**：只保留评论文本与点赞数，评论者的昵称/头像/用户 ID 一律不采集、不存储（见 `core/sanitize.py`）。
- **频控保守**：请求间随机间隔 2.5~5 秒，单次运行限量（默认 10 条视频 × 50 条评论），模拟正常浏览节奏。
- 报告为**分析引用**，不搬运、不分发原始内容；所有关键信息附来源链接。
- 请使用**采集专用小号**（自动化访问违反平台用户协议，账号可能被风控，勿用主力号）。
- 本项目仅供个人学习研究；商用前需重新评估合规并咨询专业意见。

## 开源与密钥安全设计

- 仓库中**永远不出现任何密钥**：API Key 只存在用户本机的系统凭据管理器中（`core/credentials.py`），配置向导在首次运行时引导每个用户填自己的 Key；
- Key 粘贴输入**不回显**（getpass），保存前先做真实调用验证，无效 Key 不落库；
- `.gitignore` 已排除 `.env`、`data/`、`browser_profile/`（含登录态）等所有本地敏感数据；
- 报告输出带免责声明与来源链接，原始内容不落库不分发。

## 安装

```bash
pip install -r requirements.txt
```

需要 Chrome 或 Edge 浏览器（代码会自动检测，Windows 上通常开箱即用）。

## 配置 API Key（首次运行一次即可）

双击 `运行.bat`，首次会自动弹出配置向导：

1. 选择服务商（阿里百炼 / DeepSeek / 智谱 / Moonshot / 自定义）；
2. 粘贴你自己的 API Key（**输入不回显**）；
3. 程序自动发一次最小请求验证，通过后保存。

**Key 存储在操作系统级的加密凭据库中**（Windows 凭据管理器 / macOS 钥匙串），
不写入任何项目文件，不存在被提交到 Git 或随项目分享出去的可能。

- 重新配置：`运行.bat setup`（或 `python run_cli.py setup`）；
- 备选方式：也可以把 `​.env.example` 复制为 `.env` 手工填写（明文文件，已被 `.gitignore` 排除，安全性弱于凭据管理器）。

## 首次运行（抖音扫码，一次即可）

```bash
python main.py 西湖攻略 --no-llm
```

- 会弹出浏览器窗口，用**采集小号**扫码登录抖音（登录态保存在 `browser_profile/`，之后无需重复扫码）。
- 该命令只采集原始数据，结果存到 `data/raw/西湖攻略_时间戳.json`。

## 日常运行

```bash
python main.py 西湖攻略            # 采集 + 生成攻略报告
python main.py 武功山攻略 --limit 15  # 自定义采集量
```

或直接双击 `运行.bat`，按提示输入关键词。报告输出到 `data/reports/`。

## 服务模式（网页界面，推荐）

双击 `运行服务.bat`（或 `python run_server.py`），浏览器自动打开 `http://127.0.0.1:8000`：

- 输入景点关键词 → 实时进度 → 页面直接阅读渲染好的报告；
- **景点知识库**：采集成果沉淀在本地 SQLite（`data/knowledge.db`），保鲜期内（默认 7 天，`KB_TTL_DAYS` 可配）重复查询免重爬，约 30 秒出报告；勾选"强制重新采集"可跳过缓存；
- API 形态（可供其他程序/小程序调用）：`POST /api/research`、`GET /api/jobs/{id}`、`GET /api/health`。

## 目录结构

```
├── main.py              # CLI 入口：搜索 → 采集 → LLM → 报告
├── run_server.py        # 服务入口：python run_server.py（或双击 运行服务.bat）
├── api_server.py        # FastAPI：网页 + 任务接口
├── run_cli.py           # 双击运行.bat 的交互入口（含 API Key 配置向导）
├── report_from_raw.py   # 从已采集 JSON 重新生成报告（不重爬）
├── config.py            # 频控/限量/LLM/知识库配置（.env 可覆盖）
├── core/
│   ├── models.py        # VideoItem / Comment / InfoPoint 统一数据模型
│   ├── credentials.py   # API Key 安全存取（系统凭据管理器）+ 配置向导
│   ├── knowledge.py     # 景点知识库（SQLite）
│   ├── rate_limiter.py  # 随机间隔频控
│   ├── sanitize.py      # 评论去标识化 + 文本清洗（合规闸口）
│   └── llm.py           # OpenAI 兼容客户端（JSON/重试/思考模式控制）
├── crawler/
│   ├── browser.py       # 浏览器会话、登录态管理（Chrome/Edge 自动检测）
│   └── douyin.py        # 抖音适配器：搜索 / 视频详情 / 评论（选择器集中管理）
├── pipeline/
│   ├── extract.py       # Map：逐条视频提取要点（防幻觉 + 噪声过滤）
│   ├── verify.py        # 多源交叉验证 + 置信度分级
│   ├── report.py        # Reduce：汇总生成报告正文
│   └── render.py        # 报告组装（溯源清单 + 免责声明）
├── service/
│   └── research.py      # 任务编排：知识库判定 → 采集/复用 → 提取 → 验证 → 报告
├── web/
│   └── index.html       # 网页界面
└── data/
    ├── knowledge.db     # 景点知识库
    ├── raw/             # 原始采集 JSON
    ├── reports/         # 生成的攻略报告
    └── debug/           # 页面处理失败时的 HTML 快照
```

## 页面改版了怎么办

抖音网页结构调整会导致选择器失效，表现为"搜到 0 条"或"评论 0 条"。处理方式：

1. 查看 `data/debug/` 下的失败页面 HTML 快照；
2. 浏览器 F12 确认新版页面结构；
3. 只改 `crawler/douyin.py` 顶部的 `SEL_*` 选择器常量，业务逻辑不用动。

## 已知限制（第一阶段刻意不做）

- 无字幕视频的语音内容未提取（ASR 转写在二期）；
- 多源交叉验证基于单次 LLM 语义聚类（`pipeline/verify.py`），按"不同视频来源数"判定置信度，极端情况可能漏分组；
- 单机单账号顺序采集，速度约 10 条视频 / 5 分钟。
