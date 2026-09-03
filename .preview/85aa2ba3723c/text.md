<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>开源与商业化分析 · AI 旅游助手</title>
<style>
  :root{
    --text:#1A1B1C;--sub:#5f6368;--line:#e6e4dc;--bg:#F4F3EE;--card:#fff;
    --accent:#3E8FBF;--accent-soft:#E8F3FA;
    --bad:#D9534F;--bad-soft:#FCEBEA;--warn:#E8920C;--warn-soft:#FCF3E2;
    --ok:#3E9E55;--ok-soft:#EAF6ED;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:'PingFang SC','Segoe UI','Microsoft YaHei',Arial,sans-serif;line-height:1.65;font-size:15px}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 18px 64px}
  header.hero{background:linear-gradient(135deg,#234b5e,#3E8FBF);color:#fff;border-radius:18px;padding:28px 26px;margin-bottom:22px}
  header.hero h1{margin:0 0 8px;font-size:24px}
  header.hero p{margin:4px 0;opacity:.94;font-size:14px}
  h2{font-size:20px;margin:34px 0 14px;padding-left:11px;border-left:5px solid var(--accent)}
  h3{font-size:16.5px;margin:18px 0 9px}
  .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:14px 0}
  .grid{display:flex;flex-wrap:wrap;gap:13px}
  .kpi{flex:1 1 210px;min-width:0;border:1px solid var(--line);background:#fff;border-radius:13px;padding:14px 16px}
  .kpi .t{font-size:12.5px;color:var(--sub)}
  .kpi .v{font-size:15.5px;font-weight:600;margin-top:5px;line-height:1.5}
  table{width:100%;border-collapse:collapse;font-size:13.7px;background:#fff;border-radius:12px;overflow:hidden}
  th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top}
  th{background:#eef3f6;font-weight:600}
  .tag{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:20px;white-space:nowrap}
  .t-bad{background:var(--bad-soft);color:var(--bad)} .t-warn{background:var(--warn-soft);color:var(--warn)}
  .t-ok{background:var(--ok-soft);color:var(--ok)} .t-info{background:var(--accent-soft);color:#1f5f86}
  .issue{border:1px solid var(--line);border-left:5px solid var(--bad);border-radius:11px;padding:12px 16px;margin:11px 0;background:#fff}
  .issue.warn{border-left-color:var(--warn)} .issue.ok{border-left-color:var(--ok)}
  .issue h4{margin:0 0 6px;font-size:15px}
  .issue p{margin:5px 0;font-size:13.7px}
  .issue .fix{color:#236b36} .issue .fix b{color:#1f4d6b}
  .mono{font-family:Consolas,monospace;font-size:12.3px;background:#f3f2ee;padding:1px 6px;border-radius:5px;color:#7a4a00;word-break:break-all}
  .chart{width:100%;height:380px;min-width:0}
  .chart.sm{height:330px}
  .note{font-size:12.6px;color:var(--sub);margin:6px 0}
  .vs2{display:flex;flex-wrap:wrap;gap:13px;margin-top:10px}
  .vs2>div{flex:1 1 300px;min-width:0;border-radius:13px;padding:15px 17px;background:#fff;border:1px solid var(--line)}
  .vs2 .open{border-top:5px solid var(--ok)} .vs2 .close{border-top:5px solid var(--warn)}
  .vs2 h4{margin:0 0 8px} .vs2 ul{margin:0;padding-left:18px;font-size:13.6px} .vs2 li{margin:6px 0}
  .step{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
  .step .s{flex:1 1 150px;min-width:0;background:#fff;border:1px solid var(--line);border-radius:11px;padding:12px 14px;font-size:13.2px}
  .step .s b{display:block;color:var(--accent);margin-bottom:4px;font-size:13.5px}
  .chk{list-style:none;padding:0;margin:8px 0}
  .chk li{background:#fff;border:1px solid var(--line);border-radius:9px;padding:9px 13px;margin:7px 0;font-size:13.7px}
  .chk li b{color:#1f4d6b}
  .road{display:flex;flex-wrap:wrap;gap:13px;margin-top:10px}
  .phase{flex:1 1 250px;min-width:0;border-radius:13px;padding:15px 17px;background:#fff;border:1px solid var(--line);border-top:5px solid var(--bad)}
  .phase.p1{border-top-color:var(--warn)} .phase.p2{border-top-color:var(--accent)}
  .phase h4{margin:0 0 8px;font-size:15px} .phase ul{margin:0;padding-left:18px;font-size:13.3px} .phase li{margin:5px 0}
  blockquote.q{margin:10px 0;padding:12px 16px;border-left:4px solid var(--accent);background:var(--accent-soft);border-radius:0 10px 10px 0;font-size:14px}
  footer{font-size:12.5px;color:var(--sub);margin-top:26px;border-top:1px solid var(--line);padding-top:14px}
  @media(max-width:560px){.wrap{padding:14px 11px 40px}header.hero{padding:20px 17px}header.hero h1{font-size:19px}.chart{height:320px}th,td{padding:7px 8px;font-size:12.7px}}
</style>
</head>
<body>
<div class="wrap">

<header class="hero">
  <h1>开源与商业化分析：槽点、Star 钩子与开源边界</h1>
  <p>对象：TravelGuide Assistant（抖音评论驱动的避坑型 AI 行程规划器，Python / FastAPI / MCP / 浏览器采集）</p>
  <p>视角：开源传播（Star 转化）× 工程可信度 × 商业化路径 ｜ 2026-09-03</p>
</header>

<h2>一、一句话总判断</h2>
<div class="card">
  <blockquote class="q" style="margin-top:0">
    <b>这是一个"内核 80 分、门面 30 分"的项目。</b>代码分层、811 行离线测试、CI、去标识化/置信度/营销号识别这些<b>内功在个人开源项目里属于前 10%</b>；但访客根本看不到这些——README 245 行没有一张截图/GIF、没有在线 Demo、没有样例产物，而想真正跑起来要"自备 LLM Key + 抖音小号扫码 + 等 5~40 分钟 + 承担风控风险"，并且北京样例行程自己就排崩了（热度第一的环球影城被丢、预算漏算 528 门票）。<b>开源不是把代码传上去，是经营"3 秒看懂、30 秒跑通、3 分钟被震到"的转化漏斗——你现在卡在第一关。</b>
  </blockquote>
  <p style="margin-bottom:0">商业化上要清醒：<b>旅行规划是超低频工具，纯 C 端会员养不活项目</b>；真正值钱的是「多平台 UGC 观点挖掘 + 口碑持续监控」的数据/技术资产，以及 Agent 时代可复用的"评论→立场→交叉验证→置信度"Pipeline。开源是获客与技术品牌手段，不是生意本身。</p>
</div>

<h2>二、开源就绪度：评分与 Star 转化漏斗</h2>
<div class="card">
  <h3 style="margin-top:0">2.1 八维就绪度雷达（现状 vs 开源及格线 3 分）</h3>
  <div id="radar" class="chart"></div>
  <p class="note">评分为基于仓库现状的人工评估（1–5 分）。架构与合规设计明显过线，<b>演示体验、社区基建、成品说服力三项严重拖后腿</b>——而这三项恰恰决定 Star。</p>
</div>
<div class="card">
  <h3 style="margin-top:0">2.2 典型访客的 Star 转化漏斗（每 100 个点进仓库的人）</h3>
  <div id="funnel" class="chart sm"></div>
  <p class="note">漏斗各环节为基于开源项目普遍转化率的<b>示意估算</b>，用于定位流失点，非实测数据。最大出血点在"首屏看懂"和"跑起来"两关，都是可修复的工程/运营问题，不是产品方向问题。</p>
</div>
<div class="grid">
  <div class="kpi"><div class="t">首屏 3 秒</div><div class="v" style="color:var(--bad)">无截图/无 GIF/无 Demo，约 60% 直接走</div></div>
  <div class="kpi"><div class="t">跑通门槛</div><div class="v" style="color:var(--bad)">Key+抖音小号+扫码+20 分钟，再流失 80%</div></div>
  <div class="kpi"><div class="t">成品说服力</div><div class="v" style="color:var(--warn)">样例行程有硬伤，等于公开处刑</div></div>
  <div class="kpi"><div class="t">内功</div><div class="v" style="color:var(--ok)">分层/811 测试/CI/合规设计，是真资产</div></div>
</div>

<h2>三、槽点清单：每一条都在掉 Star（附改法）</h2>

<h3>A. 演示与门槛（最致命，决定有没有人往下看）</h3>
<div class="issue">
  <h4><span class="tag t-bad">槽点 1</span>README 245 行全是文字，零截图、零 GIF、零样例报告、零在线体验</h4>
  <p>开源仓库首屏 = 电商详情页主图。访客需要在 3 秒内看到"输入城市→进度→可视化路书/避坑卡/热度榜"长什么样，文字描述再细也不如一张动图。</p>
  <p class="fix"><b>改法：</b>① README 顶部放 30–45 秒 GIF（表单→实时日志→HTML 路书：时间线/预算饼图/避坑卡/热度条）；② 放 2–3 张成品报告长图；③ 把已生成的北京/大同 HTML 报告用 GitHub Pages 挂成<b>静态在线样例</b>（无需后端即可浏览，零成本）。</p>
</div>
<div class="issue">
  <h4><span class="tag t-bad">槽点 2</span>"想看到效果"的路径太长太重：LLM Key + 抖音小号 + 扫码登录 + 5~40 分钟 + 风控风险</h4>
  <p>绝大多数潜在 Star 用户（旅行者、甚至大部分开发者）没有采集小号、不愿等、看到"账号可能被风控"就关页面。<span class="mono">data/</span> 还被 gitignore，clone 下来是空的，连成品都看不到。</p>
  <p class="fix"><b>改法：</b>① 内置 <span class="mono">--demo</span> 离线模式：随仓库带 1–2 份<b>脱敏样例 JSON + 已渲染报告</b>，零 Key 零采集 30 秒出图；② README 第一行就给"不想配置？先看 Demo / 先跑样例"按钮；③ 把"完整在线能力"和"先尝后买"分成两条路径。</p>
</div>
<div class="issue">
  <h4><span class="tag t-bad">槽点 3</span>抖音浏览器采集是平台 ToS 灰区，天然压缩受众、还埋下 DMCA/口碑风险</h4>
  <p>"违反平台用户协议、需采集小号、可能被风控"写在 README 最前面，合规意识是对的，但也等于告诉一半用户"这不适合你"。公司用户、海外用户、保守型开发者会直接放弃。</p>
  <p class="fix"><b>改法：</b>架构上把"采集器"降级为<b>可插拔适配器</b>：主流程支持用户粘贴视频链接/评论、导入 CSV、接官方/合规数据源；抖音采集器作为"用户自担风险的可选插件"独立说明。主仓库保持干净，叙事从"爬虫项目"升级为"UGC 口碑分析引擎，抖音是第一个数据源"。</p>
</div>

<h3>B. 成品质量（开源用户第一件事就是挑刺）</h3>
<div class="issue">
  <h4><span class="tag t-bad">槽点 4</span>对外样例行程自己排崩了，这是开源前的"公开处刑"风险</h4>
  <p>北京案例：调研 8 景点只排 4 个、热度第一的环球被丢、预算漏算 528 门票却写"主要花在环球"、两晚全空、最佳时段错配；大同案例：2 天总预算 255 元（7 元凉粉当全部正餐人均）。任何用户跑第一个例子都会撞上，issue 区会被淹没。</p>
  <p class="fix"><b>改法（开源前 P0）：</b>按上一份《项目优化分析》的 P0 项修复——规划校验-重排闭环、预算官方事实校准、避坑/热度只取已选点、小时段与开放时间约束。<b>先修内功再开源，顺序不能反。</b></p>
</div>

<h3>C. 定位与叙事</h3>
<div class="issue warn">
  <h4><span class="tag t-warn">槽点 5</span>名字 TravelGuide Assistant 太泛：搜不到、记不住、重名一堆</h4>
  <p class="fix"><b>改法：</b>名字/Tagline 直接打差异化情绪钩子——你真正的差异是"拆台网红景点、反营销号、差评雷达"。例如定位语：<i>"不告诉你哪好玩，专门告诉你哪是坑——用真实评论给网红景点照妖镜"</i>。仓库名可保留，项目代号/标题做出记忆点，配 GitHub Topics（travel llm-agent rag douyin review-mining mcp 等）。</p>
</div>
<div class="issue warn">
  <h4><span class="tag t-warn">槽点 6</span>README 面向"实现者"而非"用户"：频控、凭据管理器、MCP、Qoder skill 全堆在前面</h4>
  <p>普通用户不关心 keyring 怎么存 Key，开发者要到很后面才看到 Pipeline 价值。信息架构错位。</p>
  <p class="fix"><b>改法：</b>README 重排为"效果动图 → 一句话差异 → 30 秒 Demo → 安装 → 使用 → <b>它是怎么做到的（Pipeline 架构图，给开发者）</b> → 合规声明 → 二次开发"。把 MCP/OpenAPI/Qoder 收进"给 AI 开发者"折叠区，它们是技术受众的钩子，不是首屏主角。</p>
</div>
<div class="issue warn">
  <h4><span class="tag t-warn">槽点 7</span>低估了自己的技术受众：只按"旅游工具"叙事，浪费了更大的 Star 盘</h4>
  <p>"评论→立场提取→多源交叉验证→置信度分级→营销号识别→报告"这套东西，本质是<b>可复用的 UGC 观点/口碑挖掘 Pipeline，旅游只是第一个 Case</b>。做 RAG、舆情、Agent、电商评价分析的开发者远比"要去北京玩的人"多，也更爱 Star 学习。</p>
  <p class="fix"><b>改法：</b>双轨叙事——C 端讲"避坑旅行"，开发者端讲"可复用的评论观点挖掘与可信度分级框架"，README 给一张清晰的数据流架构图，把纯函数模块（verify/heat/extract）标注为"与旅游解耦、可独立复用"。</p>
</div>

<h3>D. 工程与部署（决定"装得上、跑得稳、有人贡献"）</h3>
<div class="issue">
  <h4><span class="tag t-bad">槽点 8</span>平台事实绑定 Windows，但 CI 跑 ubuntu——跨平台路径没被真验证</h4>
  <p>README/安装全是 .bat、Chrome/Edge 自动检测；Linux 下 DrissionPage 浏览器路径、keyring 依赖 SecretService、faster-whisper 的系统库都可能踩坑。Mac/Linux 用户会提一堆"跑不起来"issue。</p>
  <p class="fix"><b>改法：</b>补 macOS/Ubuntu 实测文档；提供 <b>Dockerfile / devcontainer</b>（浏览器+依赖一把装好，"试一把"转化率最高）；CI 增加 import 级跨平台冒烟。</p>
</div>
<div class="issue warn">
  <h4><span class="tag t-warn">槽点 9</span>依赖又重又不规范：faster-whisper 带几百 MB 模型却默认进 requirements；非标准包、版本上限缺失</h4>
  <p class="fix"><b>改法：</b>改 <span class="mono">pyproject.toml</span>，核心依赖与可选依赖分组（<span class="mono">[asr]</span> 放 faster-whisper、<span class="mono">[server]</span> 放 fastapi），默认 <span class="mono">pip install</span> 只装最小集；锁定主要依赖版本上限，避免 openai 等大版本漂移炸仓。</p>
</div>
<div class="issue warn">
  <h4><span class="tag t-warn">槽点 10</span>社区基建几乎为零：无 CONTRIBUTING、Issue/PR 模板、CHANGELOG、架构文档、正式 Release</h4>
  <p>README 说"预留小红书适配器"，但没有适配器接口规范和贡献指南，外部想帮你加数据源都无从下手——白白损失生态贡献。</p>
  <p class="fix"><b>改法：</b>补 CONTRIBUTING.md（重点写<b>如何加一个数据源适配器</b>，这是最好的贡献钩子）、bug/feature issue 模板、PR 模板、docs/ 架构说明、打 v0.1 Release 并附二进制/样例包。</p>
</div>
<div class="issue warn">
  <h4><span class="tag t-warn">槽点 11</span>安全红线：开源前必须扫 Git 历史</h4>
  <p><span class="mono">.gitignore</span> 现在排除了 .env / data / browser_profile 是对的，但要确认<b>历史 commit 里从未提交过 API Key、浏览器登录态（browser_profile）、原始评论数据</b>。一旦泄漏过，光改 .gitignore 没用，必须清历史+轮换 Key。</p>
  <p class="fix"><b>改法：</b><span class="mono">git log --all --full-history -- .env browser_profile data</span> 全量排查；用 gitleaks 扫一遍；确认提交历史干净再公开仓库。</p>
</div>

<h2>四、你已经做对、要放大宣传的加分项</h2>
<div class="grid">
  <div class="kpi"><div class="t">工程内功</div><div class="v" style="color:var(--ok)">清晰分层 + 811 行离线测试 + GitHub Actions 全绿徽章，可信度拉满</div></div>
  <div class="kpi"><div class="t">合规设计（稀缺）</div><div class="v" style="color:var(--ok)">评论去标识化、系统凭据库存 Key、保守频控、引文防幻觉校验——多数同类项目根本没有</div></div>
  <div class="kpi"><div class="t">三端可达</div><div class="v" style="color:var(--ok)">CLI + FastAPI（自动 OpenAPI）+ MCP Server，天然适配 Dify/Coze/Cursor 生态</div></div>
  <div class="kpi"><div class="t">可信度机制</div><div class="v" style="color:var(--ok)">置信度分级 + 营销号占比 + 情感趋势 + 来源可点验，这是差异化的技术内核</div></div>
  <div class="kpi"><div class="t">产品完成度</div><div class="v" style="color:var(--ok)">异步任务/可取消/历史落库/知识库缓存/MD+HTML 双输出/离线降级，完成度高于多数玩具项目</div></div>
  <div class="kpi"><div class="t">小白友好</div><div class="v" style="color:var(--ok)">双击 install.bat + 配置向导，Windows 非程序员也能用（保留并扩展到跨平台）</div></div>
</div>

<h2>五、开源边界：什么该开源，什么别开源</h2>
<p class="note">核心原则：<b>"建立信任、形成生态、不是护城河"的部分全部开源；"数据壁垒、持续服务、承担合规风险"的部分握在手里。</b>避坑类产品闭源反而没人信——开源 + 来源可溯本身就是卖点。</p>
<div class="vs2">
  <div class="open">
    <h4 style="color:var(--ok)">✔ 应该开源（换信任 / 换贡献 / 换传播）</h4>
    <ul>
      <li><b>本地 Pipeline 全框架</b>：extract/verify/heat/planner 纯函数、置信度与营销号识别算法——这是技术品牌，越多人看越可信</li>
      <li><b>报告模板与可视化</b>（MD/HTML/ECharts）、MCP/OpenAPI 接口</li>
      <li><b>数据源适配器接口 + 抖音适配器参考实现</b>：让社区帮你加小红书/大众点评/马蜂窝，生态长在你定义的接口上</li>
      <li><b>合规模块</b>（去标识化/频控/凭据管理）：既是卖点也是行业标杆叙事</li>
      <li><b>BYOK（用户自带 Key）</b>：继续保持，不做代付 Key 的脏活</li>
    </ul>
  </div>
  <div class="close">
    <h4 style="color:var(--warn)">⛔ 该握住 / 云化 / 谨慎的部分</h4>
    <ul>
      <li><b>多平台聚合后的清洗数据库与热度快照</b>：持续积累的数据才是真壁垒，开源本地单机缓存、闭源云端聚合资产</li>
      <li><b>托管采集服务</b>：替用户"免小号、免等待、免风控"的云端采集能力（卖便利，同时把合规风险集中管理）</li>
      <li><b>持续监控/推送服务</b>：景点口碑变化、热度涨跌订阅——本地开源版做不到"持续在线"，天然付费点</li>
      <li><b>具体反风控手段</b>：只讲合规原则，不开源绕过风控的细节，避免主仓库法律风险</li>
      <li><b>精排模型/运营权重</b>：云端排序与商业化策略不随仓库公开</li>
    </ul>
  </div>
</div>

<h2>六、商业化路径：五条路的优先级与现实判断</h2>
<div class="card">
  <div id="biz" class="chart"></div>
  <p class="note">二维评估（示意，基于行业常识）：横轴=落地可行性，纵轴=商业吸引力/天花板，气泡大小=与现有代码的复用度。</p>
</div>
<table>
  <tr><th style="width:170px">路径</th><th>判断（锐评）</th><th style="width:120px">建议</th></tr>
  <tr><td><b>① 云端免爬 SaaS</b><br>网页直接出报告</td><td>卖的是"省 40 分钟 + 不用养小号 + 不担风控"，这是用户最痛的点，也是开源到付费最顺的转化；难点是采集合规与服务器成本，要用缓存/样例/限额控成本</td><td><span class="tag t-ok">优先做</span></td></tr>
  <tr><td><b>② 多平台数据聚合 + 口碑监控订阅</b><br>热度/差评变化推送</td><td>真正的数据壁垒：本地开源版只能一次性看，云端能"持续盯"。对旅行者是出行前提醒，对商家/文旅是舆情看板，可月费</td><td><span class="tag t-ok">优先做</span></td></tr>
  <tr><td><b>③ 小程序 + PDF/长图导出</b><br>用完即走、精美交付</td><td>旅行决策低频但刚需，小程序比本地部署符合真实场景；PDF/长图/行中行程卡是明确的小额付费点，开发成本低</td><td><span class="tag t-info">低成本快做</span></td></tr>
  <tr><td><b>④ MCP / Agent 工具市场</b><br>Dify/Coze/Cursor 上架</td><td>不直接赚钱，但是最高效的<b>技术品牌与流量入口</b>：Agent 生态正缺"带来源、带置信度"的口碑工具，容易被集成、被讨论、反向带 Star</td><td><span class="tag t-info">马上做（引流）</span></td></tr>
  <tr><td><b>⑤ B 端舆情看板</b><br>景区/民宿/街区/文旅局</td><td>客单价高、决策慢、需要销售，个人开发者短期难啃；但"评论观点挖掘 Pipeline"可作为技术外包/被并购的期权，长期保留</td><td><span class="tag t-warn">观望</span></td></tr>
  <tr><td><b>⑥ 纯 C 端会员订阅</b><br>攻略功能付费墙</td><td><b>不建议作为主模型</b>：旅行规划一年用 1–2 次，留存极差，为低频工具充会员的意愿极低；付费点应放在"数据/便利/交付物"而非功能本身</td><td><span class="tag t-bad">别押注</span></td></tr>
</table>
<blockquote class="q">现实判断：<b>开源项目 → 流量与技术品牌 → 云端便利服务 + 数据监控收费 → B 端/并购期权</b>，这是最顺的链条。别指望"旅游攻略会员"本身赚钱，它是入口不是生意。</blockquote>

<h2>七、Star 增长打法</h2>
<div class="step">
  <div class="s"><b>1. 重定位受众</b>主盘不是"要旅游的人"（低频、散），而是"做 LLM/RAG/Agent/舆情的开发者"（高频逛 GitHub、爱 Star 学习）+ "被网红攻略坑过、有情绪共鸣的年轻人"（负责传播）。</div>
  <div class="s"><b>2. 情绪钩子标题</b>"专门拆台网红景点的 AI""100 条抖音评论照出谁在说谎""拒绝营销号：给旅游攻略装差评雷达"——避坑/反套路天然有传播力。</div>
  <div class="s"><b>3. 渠道节奏</b>先发即刻/V2EX（AI 独立开发圈，吃工程质量）→ 少数派/B站（做"我如何用代码识别营销号"过程视频）→ 小红书/抖音（避坑榜单内容，吃情绪）→ 投 HelloGitHub / 掘金首页 → 蹭 Agent/MCP 话题。</div>
  <div class="s"><b>4. 内容资产</b>每周发一个"城市避坑榜"（你本来就能产出），榜单末尾引流到开源工具，形成"内容→工具→Star"闭环；热度榜本身就是可持续的流量内容。</div>
  <div class="s"><b>5. 贡献者飞轮</b>把"加一个数据源/一个城市模板"降到半小时能上手，good first issue 标好，适配器贡献者会主动帮你扩平台。</div>
</div>

<h2>八、开源前必做清单（按顺序）</h2>
<div class="road">
  <div class="phase"><h4>P0 · 公开前必须（1–2 周）</h4><ul>
    <li>修掉行程规划 P0 硬伤（校验闭环/预算校准/避坑归属/时段），换掉会"公开处刑"的样例</li>
    <li>Git 历史安全扫描（Key/登录态/原始评论），gitleaks 过一遍</li>
    <li>README 首屏：GIF + 在线样例（GitHub Pages）+ 30 秒 --demo 离线模式</li>
    <li>随仓库带 1–2 份脱敏样例数据与成品报告</li>
    <li>LICENSE/作者信息、GitHub Topics、About 一句话、官网链接补齐</li>
  </ul></div>
  <div class="phase p1"><h4>P1 · 公开后两周内</h4><ul>
    <li>README 信息架构重排 + Pipeline 架构图；改名/Tagline 定调</li>
    <li>pyproject 依赖分层、锁版本；Dockerfile / devcontainer</li>
    <li>macOS/Ubuntu 实测，CI 补跨平台冒烟</li>
    <li>CONTRIBUTING + 数据源适配器开发指南 + Issue/PR 模板</li>
    <li>打 v0.1 Release、CHANGELOG</li>
  </ul></div>
  <div class="phase p2"><h4>P2 · 增长期持续</h4><ul>
    <li>MCP/OpenAPI 上架 Agent 工具市场引流</li>
    <li>小红书/点评适配器（社区共建）、i18n</li>
    <li>云端免爬版落地页（开源→SaaS 转化入口）</li>
    <li>每周城市避坑榜内容运营</li>
    <li>口碑监控/变化推送的最小付费闭环验证</li>
  </ul></div>
</div>

<footer>
  评估依据：仓库源码（README/PRD/config/api_server/web/requirements/CI/LICENSE/.gitignore）、两份成品报告（北京/大同）、上一份《项目优化分析》。评分为工程与产品经验判断，漏斗与商业矩阵为示意模型，用于定位优先级而非精确预测；市场结论请结合自身资源再决策。
</footer>

</div>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>
(function(){
  function fb(id,msg){var el=document.getElementById(id);if(el)el.innerHTML='<div style="padding:14px;color:#5f6368;font-size:13px">'+msg+'</div>';}
  if(typeof echarts==='undefined'){
    fb('radar','图表库未加载：八维就绪度——架构4.5/合规4/文档3.5/差异化3，演示1/社区1/跨平台2/成品2。');
    fb('funnel','图表库未加载：Star 漏斗 100→40→8→4→2。');
    fb('biz','图表库未加载：商业路径优先级见下方表格。');
    return;
  }
  try{
    var r=echarts.init(document.getElementById('radar'));
    r.setOption({
      backgroundColor:'transparent',
      title:{text:'开源就绪度八维评估（1–5 分）',left:'center',textStyle:{fontSize:15,color:'#1A1B1C'}},
      tooltip:{trigger:'item',triggerOn:'click',renderMode:'richText',confine:true},
      legend:{top:30,itemWidth:14,itemHeight:9,textStyle:{fontSize:11,color:'#5f6368'}},
      radar:{center:['50%','58%'],radius:'62%',
        indicator:[
          {name:'代码架构/测试',max:5},{name:'合规安全设计',max:5},{name:'文档完整度',max:5},
          {name:'差异化叙事',max:5},{name:'成品说服力',max:5},{name:'跨平台/部署',max:5},
          {name:'社区基建',max:5},{name:'演示体验',max:5}],
        axisName:{color:'#37414a',fontSize:12},splitLine:{lineStyle:{color:'#e3e1d8'}},splitArea:{areaStyle:{color:['#fbfaf6','#f4f3ee']}}},
      series:[{type:'radar',data:[
        {value:[4.5,4,3.5,3,2,2,1,1],name:'现状',areaStyle:{color:'rgba(217,83,79,.22)'},lineStyle:{color:'#D9534F',width:2},itemStyle:{color:'#D9534F'}},
        {value:[3,3,3,3,3,3,3,3],name:'开源及格线',areaStyle:{color:'rgba(62,143,191,.12)'},lineStyle:{color:'#3E8FBF',type:'dashed',width:2},itemStyle:{color:'#3E8FBF'}}
      ]}]
    });
    var f=echarts.init(document.getElementById('funnel'));
    f.setOption({
      backgroundColor:'transparent',
      title:{text:'每 100 个访客的 Star 转化漏斗（示意）',left:'center',textStyle:{fontSize:15,color:'#1A1B1C'}},
      tooltip:{trigger:'item',triggerOn:'click',renderMode:'richText',confine:true,formatter:function(p){return p.name+'：约 '+p.value+' 人';}},
      series:[{type:'funnel',left:'14%',right:'14%',top:44,bottom:8,minSize:'30%',
        label:{show:true,position:'inside',fontSize:12,formatter:function(p){return p.name+'  '+p.value;}},
        data:[
          {value:100,name:'点进仓库',itemStyle:{color:'#9fc6dd'}},
          {value:40,name:'首屏看懂价值（无截图，流失60%）',itemStyle:{color:'#7fb4d2'}},
          {value:8,name:'愿意尝试安装（门槛高，再流失80%）',itemStyle:{color:'#E8920C'}},
          {value:4,name:'成功跑通（跨平台/重依赖）',itemStyle:{color:'#e0a94e'}},
          {value:2,name:'被成品打动并 Star',itemStyle:{color:'#3E9E55'}}
        ]}]
    });
    var b=echarts.init(document.getElementById('biz'));
    b.setOption({
      backgroundColor:'transparent',
      title:{text:'五条商业化路径：吸引力 × 可行性（气泡=代码复用度）',left:'center',textStyle:{fontSize:15,color:'#1A1B1C'}},
      tooltip:{trigger:'item',triggerOn:'click',renderMode:'richText',confine:true,
        formatter:function(p){return p.data[3]+String.fromCharCode(10)+'可行性 '+p.data[0]+' / 吸引力 '+p.data[1];}},
      grid:{left:46,right:30,top:56,bottom:48,containLabel:true},
      xAxis:{name:'落地可行性 →',min:0,max:10,nameLocation:'middle',nameGap:28,splitLine:{lineStyle:{color:'#eeeae0'}},axisLabel:{fontSize:11}},
      yAxis:{name:'商业吸引力 →',min:0,max:10,nameGap:20,splitLine:{lineStyle:{color:'#eeeae0'}},axisLabel:{fontSize:11}},
      series:[{type:'scatter',symbolSize:function(d){return d[2];},
        label:{show:true,formatter:function(p){return p.data[3];},position:'top',fontSize:11.5,color:'#333'},
        itemStyle:{opacity:.82},
        data:[
          [8,8,46,'①云端免爬SaaS','#3E8FBF'],
          [6,8.5,42,'②数据聚合+口碑监控','#3E9E55'],
          [8.5,5,36,'③小程序+PDF导出','#8BC8EA'],
          [9,4.5,30,'④MCP/Agent市场(引流)','#E8920C'],
          [4,6.5,34,'⑤B端舆情看板','#b08fc4'],
          [7,2.5,26,'⑥纯C端会员(不建议)','#D9534F']
        ].map(function(x){return {value:x.slice(0,4),itemStyle:{color:x[4]}};})
      }]
    });
    window.addEventListener('resize',function(){try{r.resize();f.resize();b.resize();}catch(e){}});
  }catch(e){if(window&&window.console)console.error(e);}
})();
</script>
</body>
</html>
