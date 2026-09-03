<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 旅游助手项目诊断 · 北京攻略对比与优化方案</title>
<style>
  :root{
    --text:#1A1B1C; --sub:#5f6368; --line:#e6e4dc; --bg:#F4F3EE; --card:#fff;
    --accent:#3E8FBF; --accent-soft:#E8F3FA;
    --bad:#D9534F; --bad-soft:#FCEBEA; --warn:#E8920C; --warn-soft:#FCF3E2;
    --ok:#3E9E55; --ok-soft:#EAF6ED; --ink:#2b3a42;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:'PingFang SC','Segoe UI','Microsoft YaHei',Arial,sans-serif;
    line-height:1.65;font-size:15px;-webkit-text-size-adjust:100%}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 18px 64px}
  header.hero{background:linear-gradient(135deg,#2b5870,#3E8FBF);color:#fff;border-radius:18px;padding:28px 26px;margin-bottom:22px}
  header.hero h1{margin:0 0 8px;font-size:24px;line-height:1.35}
  header.hero p{margin:4px 0;opacity:.94;font-size:14px}
  h2{font-size:20px;margin:34px 0 14px;padding-left:11px;border-left:5px solid var(--accent)}
  h3{font-size:16.5px;margin:20px 0 9px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:14px 0}
  .grid{display:flex;flex-wrap:wrap;gap:13px}
  .kpi{flex:1 1 220px;min-width:0;border-radius:13px;padding:15px 16px;border:1px solid var(--line);background:#fff}
  .kpi .t{font-size:12.5px;color:var(--sub)}
  .kpi .v{font-size:15.5px;font-weight:600;margin-top:5px;line-height:1.5}
  .tag{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:20px;margin-right:6px;white-space:nowrap}
  .t-p0{background:var(--bad-soft);color:var(--bad)}
  .t-p1{background:var(--warn-soft);color:var(--warn)}
  .t-p2{background:#EEF1F4;color:#5a6570}
  .t-ok{background:var(--ok-soft);color:var(--ok)}
  table{width:100%;border-collapse:collapse;font-size:13.8px;background:#fff;border-radius:12px;overflow:hidden}
  th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top}
  th{background:#eef3f6;font-weight:600;white-space:nowrap}
  td.dim-col-bad{background:#fdf6f5}
  td.dim-col-ok{background:#f4faf5}
  .mono{font-family:Consolas,'Courier New',monospace;font-size:12.5px;background:#f3f2ee;padding:1px 6px;border-radius:5px;color:#7a4a00;word-break:break-all}
  .issue{border:1px solid var(--line);border-left:5px solid var(--bad);border-radius:11px;padding:13px 16px;margin:12px 0;background:#fff}
  .issue.p1{border-left-color:var(--warn)}
  .issue.p2{border-left-color:#9aa6ad}
  .issue h4{margin:0 0 7px;font-size:15px}
  .issue .row{display:flex;gap:8px;margin:6px 0;font-size:13.6px}
  .issue .lab{flex:0 0 64px;font-weight:600;color:var(--sub)}
  .issue .body{flex:1;min-width:0}
  .pipe{display:flex;flex-wrap:wrap;align:stretch;gap:0;margin:10px 0}
  .node{flex:1 1 120px;min-width:110px;padding:10px 12px;border-radius:10px;font-size:12.8px;text-align:center;position:relative;margin:6px 14px 6px 0;line-height:1.45}
  .node:not(:last-child)::after{content:"→";position:absolute;right:-14px;top:50%;transform:translateY(-50%);font-weight:700;color:#8a9499;font-size:15px}
  .n-gray{background:#eef1f3;border:1px solid #d9dee1}
  .n-bad{background:var(--bad-soft);border:1px solid #f0c4c2;color:#8c2f2c;font-weight:600}
  .n-ok{background:var(--ok-soft);border:1px solid #bfe3c8;color:#236b36;font-weight:600}
  .n-key{background:var(--accent-soft);border:1.5px dashed var(--accent);color:#1f5f86;font-weight:600}
  .pipe-label{font-size:12.5px;font-weight:700;color:var(--sub);margin:14px 0 2px;letter-spacing:1px}
  /* 标杆攻略 */
  .day{border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:14px 0;background:#fff}
  .day-head{background:#eaf3f8;padding:12px 17px;font-weight:700;font-size:15.5px;color:#1f4d6b;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}
  .day-head span{font-weight:400;font-size:12.8px;color:#5a6b75}
  .tl{list-style:none;margin:0;padding:6px 0}
  .tl li{display:flex;gap:13px;padding:9px 17px;border-top:1px dashed #eceae2}
  .tl li:first-child{border-top:none}
  .tl .tm{flex:0 0 96px;font-weight:700;color:var(--accent);font-size:13.3px;padding-top:1px}
  .tl .ct{flex:1;min-width:0;font-size:13.8px}
  .pit{background:var(--bad-soft);border-radius:9px;padding:9px 13px;margin:8px 17px 13px;font-size:13.1px}
  .pit b{color:var(--bad)}
  .src{color:#8a6d3b;font-size:12px}
  .check{list-style:none;padding:0;margin:8px 0}
  .check li{background:#fff;border:1px solid var(--line);border-radius:9px;padding:9px 13px;margin:7px 0;font-size:13.7px}
  .check li b{color:#1f4d6b}
  .budget-total{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0}
  .bt{flex:1 1 150px;background:#fff;border:1px solid var(--line);border-radius:11px;padding:12px 15px}
  .bt .n{font-size:22px;font-weight:700;color:var(--accent)}
  .bt.bad .n{color:var(--bad)} .bt.good .n{color:var(--ok)}
  .bt .l{font-size:12.3px;color:var(--sub)}
  .chart{width:100%;height:330px;min-width:0}
  .note{font-size:12.6px;color:var(--sub);margin:6px 0}
  .road{display:flex;flex-wrap:wrap;gap:13px;margin-top:10px}
  .phase{flex:1 1 270px;min-width:0;border-radius:13px;padding:15px 17px;background:#fff;border:1px solid var(--line);border-top:5px solid var(--bad)}
  .phase.p1{border-top-color:var(--warn)} .phase.p2{border-top-color:var(--accent)}
  .phase h4{margin:0 0 8px;font-size:15px}
  .phase ul{margin:0;padding-left:18px;font-size:13.4px}
  .phase li{margin:5px 0}
  .vs-bad{color:var(--bad);font-weight:600} .vs-ok{color:var(--ok);font-weight:600}
  footer{font-size:12.5px;color:var(--sub);margin-top:26px;border-top:1px solid var(--line);padding-top:14px}
  @media(max-width:560px){
    .wrap{padding:14px 11px 40px}
    header.hero{padding:20px 17px} header.hero h1{font-size:19.5px}
    .tl .tm{flex-basis:78px} .node{flex-basis:44%}
    th,td{padding:7px 8px;font-size:12.8px}
  }
</style>
</head>
<body>
<div class="wrap">

<header class="hero">
  <h1>AI 旅游助手项目诊断报告<br>北京 2 天攻略：现有成品 vs 标杆重做 + 代码级优化方案</h1>
  <p>诊断对象：TravelGuide Assistant（抖音评论驱动的避坑型行程规划器）</p>
  <p>同参数对比：北京 · 2 天 · 住三环内 · 预算 1500 元/人（不含大交通与住宿）· 体验优先 · 年轻人</p>
  <p>分析样本：项目生成的《行程_北京_20260903_112343》《行程_大同_20260903》两份成品 + 全部规划链路源码</p>
</header>

<!-- ============ 0. 一句话结论 ============ -->
<h2>一、先说结论（锐评）</h2>
<div class="card">
  <p style="margin-top:0"><b>你的三个卖点方向（评论避坑、热度榜、真实评价摘要）是对的，数据管道也确实跑通了；但"最后一公里"——把调研数据编排成一份能直接出发的行程——目前是整个系统最弱的一环。</b></p>
  <p>用你项目自己北京案例的数据说话：花了约 40 分钟调研了 <b>8 个景点 + 1 家餐厅</b>（含抖音热度榜前 3 名：环球影城、国博、颐和园），最终行程只排进 <b>4 个免费/低价点、0 家餐厅、两个晚上全部空白</b>，年轻人最该去、热度第一的环球影城被丢弃，预算却得出"984 元、主要花在环球门票"这种<b>自相矛盾</b>的结论。大同案例更夸张：2 天行程总预算 255 元（一碗 7 元浑源凉粉被当成了全部正餐人均）。</p>
  <p style="margin-bottom:0">问题不在大模型"发挥失常"，而在<b>架构：规划是"一次生成、直接渲染"，没有任何校验-修复闭环；预算、避坑、热度三个模块的统计口径全部基于"调研全集"而不是"实际行程"；且为防幻觉禁止模型使用常识，又没有官方事实源兜底，导致票价等硬事实系统性缺失。</b>下文逐项给出现象、代码根因和改法。</p>
</div>

<div class="grid">
  <div class="kpi"><div class="t">景点调研利用率（北京）</div><div class="v vs-bad">4 / 8 = 50%，热度前 3 全丢</div></div>
  <div class="kpi"><div class="t">餐厅落位率</div><div class="v vs-bad">0 / 1，4 个正餐槽全空，餐饮费照算 800</div></div>
  <div class="kpi"><div class="t">时段空置</div><div class="v vs-bad">两天晚上 100% 空白，最佳时段"晚上"的亮马河被排到下午</div></div>
  <div class="kpi"><div class="t">预算可信度</div><div class="v vs-bad">北京漏算 528 元环球门票；大同 2 天 255 元</div></div>
</div>

<!-- ============ 1. 正面对比 ============ -->
<h2>二、同参数正面对比：项目版 vs 标杆重做版</h2>
<p class="note">标杆版同样吸收了你项目采集到的抖音真实评论避坑（表中标"评论"），区别在于补上了决策逻辑、官方事实核验与编排闭环。官方事实为 2026 年 9 月口径，来源见文末。</p>
<table>
  <tr><th style="width:118px">维度</th><th>项目现有成品</th><th>标杆版（你应该做到的形态）</th></tr>
  <tr><td><b>选点逻辑</b></td>
    <td class="dim-col-bad">8 个调研点只排 4 个；环球/国博/颐和园/天坛被静默丢弃，无任何解释；热度分根本没传给规划模型</td>
    <td class="dim-col-ok">先给"规划决策"：环球热度第 1 且需整天 → Day1 单独给通州；Day2 按"南→北→东北"单向动线；未入选点（798/天坛）进备选区并说明取舍</td></tr>
  <tr><td><b>时间粒度</b></td>
    <td class="dim-col-bad">只有"上午/下午"，无钟点；无开闭园约束；晚上全空</td>
    <td class="dim-col-ok">小时级时间轴（08:15 出发、09:00 入园、20:30 灯光秀…），每段时长可累加校验</td></tr>
  <tr><td><b>地理动线</b></td>
    <td class="dim-col-bad">Day1 798→亮马河尚可，但把通州环球、西北颐和园、南城天坛全部调研后丢弃，看不出排线依据</td>
    <td class="dim-col-ok">Day1 通州单点整天（跨区不混排）；Day2 国博→什刹海→五道营→亮马河一路向东北，零折返</td></tr>
  <tr><td><b>最佳时段</b></td>
    <td class="dim-col-bad">亮马河档案写"最佳时段：晚上"，行程排在下午，自相矛盾无人校验</td>
    <td class="dim-col-ok">亮马河 19:30 到（夜景正是其核心卖点）；时段匹配作为硬约束</td></tr>
  <tr><td><b>餐饮</b></td>
    <td class="dim-col-bad">只调研 1 家餐厅且没排进任何一餐；"美食：无"；但预算按 200×2 正餐×2 天=800 元照收</td>
    <td class="dim-col-ok">每一餐落到具体时间与区位（园内午餐/城市大道晚餐/胡同午餐/四季民福晚餐），花费与餐次一一对应</td></tr>
  <tr><td><b>预算</b></td>
    <td class="dim-col-bad">984 元：门票只算到天坛旧价 35 元，环球 528 元门票缺失，却写"主要花在环球门票"；两天日小计都是机械的 430 元</td>
    <td class="dim-col-ok">约 1091 元（区间 1000–1250），门票/餐费/交通逐笔对应真实行程，并说明什么情况会超支</td></tr>
  <tr><td><b>事实时效</b></td>
    <td class="dim-col-bad">9 月 3 日的报告仍把"8/6 百花奖、8/9 红毯""1.18–8.18 三星堆展"当亮点；天坛沿用旧票价 10/25 元（现为 15/34）</td>
    <td class="dim-col-ok">限时信息与当前日期比对，过期不进亮点；票价/预约走官方口径并标注核验日期</td></tr>
  <tr><td><b>避坑相关性</b></td>
    <td class="dim-col-bad">12 条避坑 9 条属于没排进去的点；大同报告混入行程里没有的悬空寺、不在清单的"老柴"</td>
    <td class="dim-col-ok">避坑只挂在当天当点；未入选点的坑收进"临时加项才看"的附录</td></tr>
  <tr><td><b>行前待办</b></td>
    <td class="dim-col-bad">国博抢票、周一闭馆等碎在贴士里，用户自己拼</td>
    <td class="dim-col-ok">独立预约清单：T-7 天 17:00 抢国博、指定日买环球、哪些公园 2026.8 起免预约</td></tr>
  <tr><td><b>应急预案</b></td>
    <td class="dim-col-bad">无雨天方案、无体力管理（评论反复预警 2 万步却不反馈到排线）、无 Plan B</td>
    <td class="dim-col-ok">给雨天替换、排队过载策略、"不去环球"的低价版分支与对应预算</td></tr>
</table>

<!-- ============ 2. 标杆攻略 ============ -->
<h2>三、标杆版成品：北京 2 天（同样参数，可直接出发）</h2>

<div class="card">
  <h3 style="margin-top:0">规划决策（先讲为什么这么排）</h3>
  <p style="margin-bottom:0">① 你要"年轻人 + 体验优先"，抖音热度第 1 是环球影城（0.95），它需要 10–12 小时整天、在东六环通州，<b>单独占 Day1 且选工作日</b>（9 月平季票 528 元，9 月 4 日后客流回升，评论与官方日历一致）；② Day2 走"<b>中轴 → 北城胡同 → 朝阳水岸</b>"一条向东北的单向线，全程不折返；③ 798、颐和园、天坛调研数据不浪费，降级为备选并给替换条件；④ 全程地铁可达，住三环内不跨区折腾。</p>
</div>

<div class="day">
  <div class="day-head">Day 1 · 北京环球度假区（通州 · 全天主题）<span>地铁 1/7 号线环球度假区站直达 · 预计花费 ≈722 元</span></div>
  <ul class="tl">
    <li><div class="tm">08:15</div><div class="ct">三环内酒店出发，地铁 1/7 号线到「环球度假区」站，约 50–60 分钟、约 7 元。<b>开园前到门口</b>，这是不买优速通的前提（评论："9:00 入园，14:00 全部项目刷完"）。</div></li>
    <li><div class="tm">09:00</div><div class="ct">开园即冲哈利波特的禁忌之旅 + 侏罗纪世界霸天虎（上午排队最短，热门先刷）。</div></li>
    <li><div class="tm">12:30</div><div class="ct">园内午餐（三把扫帚/平先生面馆，人均 80–100）。</div></li>
    <li><div class="tm">14:00</div><div class="ct">变形金刚基地 → 功夫熊猫盖世之地 → 小黄人乐园，配合官方 App 实时排队时间穿插。</div></li>
    <li><div class="tm">17:00</div><div class="ct">花车巡游/演艺（以当日官方 App 时间表为准）。</div></li>
    <li><div class="tm">20:30</div><div class="ct">霍格沃茨城堡夜间灯光秀（评论与官方均指向 20:30 前后）。</div></li>
    <li><div class="tm">21:00</div><div class="ct">城市大道晚餐+逛街（人均 80–100，比园内便宜，无需门票），后地铁返程。</div></li>
  </ul>
  <div class="pit"><b>本点避坑（来自你的抖音评论库）：</b>9 月 4 日后客流回升、热门项目排队 30–60 分钟 → 选工作日 + 开园先冲热门，1500 预算下不建议加购优速通/管家（另付数百）；胆小不敢玩刺激项目的同行人可用官方托管，不必硬陪。<span class="src">（评论来源：video/7680389726750886062、7654745667227137913）</span></div>
</div>

<div class="day">
  <div class="day-head">Day 2 · 国博 → 什刹海胡同带 → 亮马河（市区 · 单向向东北）<span>预计花费 ≈270 元</span></div>
  <ul class="tl">
    <li><div class="tm">09:00</div><div class="ct"><b>国家博物馆</b>（免费，9:00–17:30，周一闭馆）。只攻精华：负一层"古代中国" + 凤冠 + 当期特展，<b>2.5–3 小时撤出</b>，别贪全——评论"全程 5 万步"是真的，精选路线 1.5 万步内。</div></li>
    <li><div class="tm">12:15</div><div class="ct">前门/王府井方向京味午餐，人均约 80。</div></li>
    <li><div class="tm">13:30</div><div class="ct">地铁 8 号线到<b>什刹海</b>，共享单车环后海、穿南锣鼓巷（评论避坑：别坐高价三轮，骑行/步行即可，景区本身不大）。</div></li>
    <li><div class="tm">15:30</div><div class="ct">骑行约 10 分钟到<b>五道营胡同</b>+雍和宫宫外；想要更原生态，按评论指引拐北锣鼓巷/东西四头条至十二条（游客更少）。</div></li>
    <li><div class="tm">17:30</div><div class="ct"><b>晚餐：四季民福烤鸭</b>，人均 150–200。评论避坑：故宫店景观位要半夜排队 → 直接选安德路店/西站食宝街店；狮子头等争议菜品慎点。</div></li>
    <li><div class="tm">19:30</div><div class="ct">地铁到<b>亮马河国际风情水岸</b>——它的最佳时段本来就是晚上（项目版错排到下午）：滨水散步、可坐船、蓝色港湾，免费、无需预约。</div></li>
    <li><div class="tm">21:30</div><div class="ct">返程。</div></li>
  </ul>
  <div class="pit"><b>本点避坑（来自你的抖音评论库）：</b>国博免费票极难抢（"三个人抢两张，半小时没抢到"）→ 提前 7 天 17:00 微信小程序卡点卡，备选当期收费特展票；什刹海高峰期桥上拥挤 → 下午四点前到；五道营商业气息重 → 想要原生态按上面路线绕行。<span class="src">（评论来源：video/7657100723466020787、7659384181830456410、7530957066798714121）</span></div>
</div>

<div class="card">
  <h3 style="margin-top:0">行前预约 / 准备清单</h3>
  <ul class="check">
    <li><b>T-7 天 17:00</b>：微信小程序「国家博物馆」抢免费票（提前 7 天 17:00 放、秒空，定闹钟；每账号每周限约 1 次）。</li>
    <li><b>提前 3 天起</b>：官方渠道买环球<b>指定日</b>票，9 月工作日平季 528 元（节假日档 748 元，日期不同差价可达 220–330 元）。</li>
    <li><b>不用预约</b>：2026 年 8 月起，颐和园/天坛/景山等市属公园取消强制预约，现场购票刷证入园（你项目报告里"天坛需预约、大门票 10 元"是旧信息，需更新）。</li>
    <li><b>本次排不下、别硬塞</b>：故宫（提前 7 天 20:00 放票、旺季 60、周一闭馆）、颐和园（3 小时起、西北五环）列入下次；周一出行则把国博替换为天坛。</li>
    <li><b>装备</b>：评论反复预警日均 2 万步 → 运动鞋；环球带空水瓶（园内可接水）；身份证全程随身。</li>
  </ul>
</div>

<div class="budget-total">
  <div class="bt good"><div class="l">标杆版总花费（1 人）</div><div class="n">≈1091 元</div><div class="l">合理区间 1000–1250，1500 预算结余约 400</div></div>
  <div class="bt bad"><div class="l">项目版估算</div><div class="n">984 元</div><div class="l">漏算环球 528 门票，结论失真</div></div>
  <div class="bt"><div class="l">超支触发条件（明示）</div><div class="n" style="font-size:15px;color:var(--warn)">节假日出行 / 买优速通 / 园内消费失控</div><div class="l">出现任一项预算上浮 200–600</div></div>
</div>
<table>
  <tr><th>项目</th><th>Day1 环球</th><th>Day2 市区线</th><th>合计</th></tr>
  <tr><td>门票</td><td>环球指定日票 528</td><td>国博/胡同/亮马河 0</td><td><b>528</b></td></tr>
  <tr><td>餐饮</td><td>园内午+城市大道晚 ≈180</td><td>午餐 80 + 四季民福 170</td><td><b>430</b></td></tr>
  <tr><td>市内交通</td><td>地铁往返 ≈14</td><td>地铁+单车 ≈20</td><td><b>34</b></td></tr>
  <tr><td>弹性 10%</td><td colspan="2" style="text-align:center">992 × 10%</td><td><b>99</b></td></tr>
  <tr><td><b>合计</b></td><td colspan="2"></td><td><b>≈1091 元</b></td></tr>
</table>

<div class="card">
  <h3 style="margin-top:0">Plan B（应急预案，项目目前完全没有）</h3>
  <p style="margin-bottom:6px"><b>雨天：</b>环球室内项目占比高、影响小；Day2 压缩胡同骑行，国博延长 + 亮马河改蓝色港湾室内商场。</p>
  <p style="margin-bottom:6px"><b>排队过载：</b>环球官方 App 看实时等待，超 60 分钟的项目直接跳过，保 3 个核心项目。</p>
  <p style="margin-bottom:0"><b>不去环球的低价版（预算敏感）：</b>Day1 798（白天）+ 亮马河（晚），Day2 国博 + 天坛 + 胡同线，总花费降到约 550 元——把"是否去环球"做成用户可切换的分支，而不是默默丢弃。</p>
</div>

<!-- ============ 3. 问题清单 ============ -->
<h2>四、问题清单：现象 → 代码根因 → 改法</h2>

<h3>P0 · 直接导致攻略不可用 / 误导决策</h3>

<div class="issue">
  <h4><span class="tag t-p0">P0-1</span>规划"一次生成直接渲染"，无校验-修复闭环，调研成果浪费且无解释</h4>
  <div class="row"><div class="lab">现象</div><div class="body">北京 8 个调研点只排 4 个，热度前 3（环球/国博/颐和园）全丢；大同 Day2 上午、晚上全空。</div></div>
  <div class="row"><div class="lab">根因</div><div class="body"><span class="mono">service/trip.py L351</span> 只调用一次 <span class="mono">plan_itinerary</span>；<span class="mono">planner.py _normalize_plan</span> 只丢弃"不在名单"的点，不校验入选率/每日密度，不做第二次重排；且<b>热度数据根本没传进规划 prompt</b>（plan_itinerary 的 user 消息只有档案，没有 heat 分）。</div></div>
  <div class="row"><div class="lab">改法</div><div class="body">新增 <span class="mono">validate_and_repair(plan, profiles, heat)</span>：① 入选率 &lt;70% 或日均时段 &lt;2.5 → 把"未入选点 + 违反的规则"塞回 prompt 重排 1 次；② 热度分/趋势写入规划输入，高热度点被弃时必须给理由；③ 最终仍未入选的点移到"备选方案"区输出，不许静默消失。</div></div>
</div>

<div class="issue">
  <h4><span class="tag t-p0">P0-2</span>预算系统性失真，"结余"结论反向误导</h4>
  <div class="row"><div class="lab">现象</div><div class="body">北京 984 元：漏算环球 528 门票却写"主要花在环球门票"；大同 255 元/2 天（7 元浑源凉粉被当成全部正餐人均，4 顿 28 元）。</div></div>
  <div class="row"><div class="lab">根因</div><div class="body"><span class="mono">build_budget_summary</span> 四处硬伤：(a) 门票只收评论里出现的明确数字，而 <span class="mono">PROFILE_SYSTEM</span> 规则 1 禁止用常识补，评论没提票价=0，<b>无官方票价兜底</b>；(b) <span class="mono">avg_meal</span> 把小吃店与正餐店人均简单算术平均、缺失即丢，样本=1 时 7 元代表全部正餐；(c) 交通固定 15 元/点，<b>不用自己已经生成的 transport 费用</b>（798 卡片写打车 40–50，预算仍按 15）；(d) 统计口径是全部 profiles 而非实际入选 plan。</div></div>
  <div class="row"><div class="lab">改法</div><div class="body">① 票价走"官方/高德 POI 核验优先，评论估算其次，双源冲突时并列标注"；② 餐饮人均按正餐/小吃分档，缺档用城市基准区间（北京正餐 100–150），输出区间而非单点；③ 交通费从实际 plan 路段的 travel_lines 抽数值求和；④ 预算只统计实际排进 plan 的点；⑤ 给下限/上限区间，删掉伪精确。</div></div>
</div>

<div class="issue">
  <h4><span class="tag t-p0">P0-3</span>避坑专题 / 热度榜与实际行程脱节，且要点张冠李戴</h4>
  <div class="row"><div class="lab">现象</div><div class="body">北京 12 条避坑 9 条属于未入选点；大同避坑榜出现行程里没有的悬空寺、不在餐厅清单的"老柴削面"。</div></div>
  <div class="row"><div class="lab">根因</div><div class="body"><span class="mono">trip.py L359 pitfall_digest(all_points)</span> 用的是调研全集；<span class="mono">extract.py</span> 无主体归属校验——搜"云冈石窟"的视频作者顺口讲悬空寺，该要点被记到云冈名下；heat_rows 同样全量。</div></div>
  <div class="row"><div class="lab">改法</div><div class="body">① 避坑/热度默认只收入选 plan 的点，未入选点的内容进附录；② extract 输出增加 <span class="mono">about_target</span>（本景点/同城其他/通用行程），后两类不进景点档案（可根治华严寺贴士混入"4 天大同行程单"、凤临阁贴士混入"应县木塔"这类跑题污染）；③ 热度榜每行加"已选/未选"角标。</div></div>
</div>

<div class="issue">
  <h4><span class="tag t-p0">P0-4</span>时间只有上下午、无开闭园约束、最佳时段自相矛盾</h4>
  <div class="row"><div class="lab">现象</div><div class="body">亮马河最佳时段"晚上"被排下午；云冈 4 小时排下午；两天晚上全空。</div></div>
  <div class="row"><div class="lab">根因</div><div class="body"><span class="mono">ALLOWED_SLOTS</span> 只有上午/下午/晚上；PLAN 规则 1"每时段 0~2 个、要留白"被模型当成可以大面积留空；没有开放时间/游玩时长的可行性校验。</div></div>
  <div class="row"><div class="lab">改法</div><div class="body">slot 输出 start/end 小时段；把官方开放时间作为硬约束做可行性检查（时长 + 交通不得超出开放窗口）；最佳时段与实际排序冲突、或晚上空置而存在"晚上最佳"点时触发重排；按偏好设每日有效游玩时长下限（体验优先 ≥6h）。</div></div>
</div>

<h3>P1 · 显著拉低质量</h3>

<div class="issue p1"><h4><span class="tag t-p1">P1-1</span>时效信息不过期，旧票价/已结束活动照常进亮点
  <div class="row"><div class="lab">现象</div><div class="body">9/3 报告仍推荐"8/6 百花奖、8/9 红毯""1.18–8.18 三星堆展"；天坛沿用旧价 10/25（2026 已 15/34）。</div></div>
  <div class="row"><div class="lab">改法</div><div class="body">profile 阶段注入当前日期，每条信息标注"长期有效/限时（截止日）/已过期"，过期项不进 highlights；票价、开放时间类强制走官方刷新。</div></div></h4>
</div>
<div class="issue p1"><h4><span class="tag t-p1">P1-2</span>餐厅与餐次脱节：候选不足、槽位全空、费用照算、"人均未知"直接渲染
  <div class="row"><div class="lab">改法</div><div class="body">按"正餐类型 × 所在区位"配额，保证每天午/晚都有候选；plan 后校验正餐槽非空（空则重排或显式标"自理"）；预算与实际排定餐次挂钩；"人均未知"触发联网补值而非裸输出。</div></div></h4>
</div>
<div class="issue p1"><h4><span class="tag t-p1">P1-3</span>每日花费小计机械均摊（两天都是一模一样的 430 元）
  <div class="row"><div class="lab">改法</div><div class="body"><span class="mono">day_subtotals</span> 改为按当天实际 slot 花费 + 当天实际排定餐厅 + 当天真实路段交通求和，不再按天平均。</div></div></h4>
</div>
<div class="issue p1"><h4><span class="tag t-p1">P1-4</span>热度趋势/情感趋势两个卖点在行程报告里基本失效
  <div class="row"><div class="lab">现象</div><div class="body">9 个点 8 个"近期热度上升"（阈值 fresh≥0.4 过松），7 个"情感数据不足"（每侧需 5 条带时间评论，5 个视频采样根本不够）。</div></div>
  <div class="row"><div class="lab">改法</div><div class="body">行程报告复用 heatrefresh 的 <span class="mono">trend_of</span> 四态判定；情感趋势要么提高评论采样下限，要么诚实显示样本量，不用满屏"数据不足"占版面。</div></div></h4>
</div>
<div class="issue p1"><h4><span class="tag t-p1">P1-5</span>缺预约清单、缺备选方案、缺体力管理（评论预警 2 万步却不反馈排线）
  <div class="row"><div class="lab">改法</div><div class="body">新增三个固定模块：booking_checklist（预约项+提前天数+放票时间，官方核验）、contingency（雨天/排队/体力 Plan B）、每日步数/强度标签反哺密度控制。</div></div></h4>
</div>
<div class="issue p1"><h4><span class="tag t-p1">P1-6</span>"规划说明"是正确的废话，取舍不可解释
  <div class="row"><div class="lab">改法</div><div class="body">输出"规划决策记录"：为什么某点整天、为什么舍弃某点、预算如何在门票/餐饮间权衡——把你数据管道的存在显式化，这正是差异化。</div></div></h4>
</div>

<h3>P2 · 体验与工程</h3>
<div class="grid">
  <div class="kpi"><div class="t">P2-1 置信度</div><div class="v" style="font-size:13.5px;font-weight:400">12 条避坑无一"高置信"，低置信（单源）应默认折叠，按高/中/低分组而非混排</div></div>
  <div class="kpi"><div class="t">P2-2 口径提示</div><div class="v" style="font-size:13.5px;font-weight:400">预算对比处显著标注"不含住宿/往返大交通"，避免用户误判总出行成本</div></div>
  <div class="kpi"><div class="t">P2-3 跨区移动</div><div class="v" style="font-size:13.5px;font-weight:400">三环→通州环球这类长通勤强制高耗时标记，避免与市区点同天混排</div></div>
  <div class="kpi"><div class="t">P2-4 测试闸</div><div class="v" style="font-size:13.5px;font-weight:400">tests_offline 增加"入选率/预算与行程一致/时段匹配"三条断言，防回归</div></div>
</div>

<!-- ============ 4. 管线对比 ============ -->
<h2>五、根因总图：一次成型管线 vs 闭环规划管线</h2>
<div class="card">
  <div class="pipe-label">现状（问题直接流到成品）</div>
  <div class="pipe">
    <div class="node n-gray">LLM 圈定候选</div>
    <div class="node n-gray">抖音验证采集</div>
    <div class="node n-gray">档案/摘要蒸馏</div>
    <div class="node n-bad">LLM 一次排线<br>（无热度输入）</div>
    <div class="node n-bad">直接渲染<br>不校验</div>
  </div>
  <div class="note">后果：点丢了没人管、时段冲突没人管、预算按"全集"另算、避坑按"全集"另堆——四个模块各算各的。</div>

  <div class="pipe-label">目标（规划即约束求解 + 自检闭环）</div>
  <div class="pipe">
    <div class="node n-gray">圈定/验证/蒸馏</div>
    <div class="node n-gray">排线 v1<br>（输入含热度+开放时间）</div>
    <div class="node n-key">校验器<br>入选率·密度·时段·动线·预算·预约</div>
    <div class="node n-key">不达标→带反馈重排 v2</div>
    <div class="node n-ok">官方事实校准<br>（票价/时间/预约）</div>
    <div class="node n-ok">渲染<br>避坑/预算只取已选点</div>
  </div>
  <div class="note">关键：校验器是纯函数、可离线单测；重排最多 1–2 次控成本；官方数据与评论数据双轨并列、各自标注来源。</div>
</div>

<!-- ============ 5. 数据图 ============ -->
<h2>六、两张图看清失真程度</h2>
<div class="card">
  <h3 style="margin-top:0">6.1 预算结构对比（元）</h3>
  <div id="budgetChart" class="chart"></div>
  <p class="note">口径：项目版 984 元来自其报告预算表（门票只含天坛旧价 35、餐饮 200×4 顿、交通 4 点×15、弹性 89.5）；标杆版 1091 元按实际排定行程逐笔累计。项目版最大问题不是总额，而是<b>结构错配</b>：该花的门票（环球 528）缺席、吃不到的 4 顿正餐被预收 800。</p>
</div>
<div class="card">
  <h3 style="margin-top:0">6.2 北京案例候选漏斗：调研成本花了，一半没进行程</h3>
  <div id="funnelChart" class="chart" style="height:300px"></div>
  <p class="note">圈定 20 → 验证 12 → 保留 9（8 景点 + 1 餐厅）→ 最终排进行程仅 4 个景点、0 家餐厅落位。每个被丢弃的点都对应约 5 分钟采集 + 2 次 LLM 调用的真金白银成本。</p>
</div>

<!-- ============ 6. 路线图 + 差异化 ============ -->
<h2>七、优化路线图与差异化护城河</h2>
<div class="road">
  <div class="phase"><h4>P0 · 1–2 周｜解决"能不能用"</h4><ul>
    <li>规划校验-重排闭环（P0-1）</li>
    <li>预算四连修 + 官方票价兜底（P0-2）</li>
    <li>避坑/热度只取已选点 + 主体归属（P0-3）</li>
    <li>小时段 + 开放时间硬约束（P0-4）</li>
  </ul></div>
  <div class="phase p1"><h4>P1 · 2–4 周｜解决"好不好用"</h4><ul>
    <li>时效过滤、旧票价自动刷新</li>
    <li>餐位闭环、日小计实算</li>
    <li>预约清单 / Plan B / 体力强度</li>
    <li>四态趋势与情感样本诚实化</li>
  </ul></div>
  <div class="phase p2"><h4>P2 · 持续｜建护城河</h4><ul>
    <li>规划升级为约束求解器（预约窗×开放时间×动线×预算×体力联合求解）</li>
    <li>评论数据 × 官方数据双轨校准、冲突并列</li>
    <li>多轮调整 + 出发当日"今日行程卡/预约提醒/排队实况"</li>
  </ul></div>
</div>

<div class="card" style="margin-top:16px">
  <h3 style="margin-top:0">差异化判断：别停在"信息层"，要进"决策层"</h3>
  <p style="margin-bottom:0">评论避坑、热度榜、评价摘要目前都属于<b>信息罗列</b>，通用大模型 + 一次搜索很快能抄到形态。抄不走的是：<b>① 约束求解与自检闭环</b>（开放时间、预约窗口、地理动线、预算、体力、最佳时段的联合可行解，纯一次性生成做不到）；<b>② 评论体感 × 官方事实的双轨校准</b>（评论告诉你"坑长什么样"，官方保证"票价/时间是真的"，两边冲突显式并列，你恰好同时有爬虫和 LLM 联网，这是结构性优势）；<b>③ 可解释的取舍</b>（每个选/不选的点都有证据）。把这三点做成产品主叙事，"避坑"才从卖点变成壁垒。</p>
</div>

<footer>
  <b>事实来源与口径</b>（2026-09-03 核验）：环球影城四档票价 418/528/638/748、9 月平季 528、营业 09:00–21:00——北京环球度假区官网及公开票务页；国博免费实名预约、提前 7 天 17:00 放票、周一闭馆、9:00–17:30——中国国家博物馆官网；故宫旺季 60、提前 7 天 20:00 放票；2026 年 8 月起颐和园/天坛等市属公园取消强制预约、天坛联票 34——北京本地宝/新浪新闻及公开攻略交叉核验。评论类避坑均来自项目 data/raw 已采集的抖音视频要点，文内标注视频 ID。预算为 1 人、不含住宿与往返大交通口径，金额取整、给区间。<br>
  代码位置基于当前仓库版本：pipeline/planner.py、pipeline/heat.py、pipeline/candidates.py、service/trip.py。
</footer>

</div>

<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>
(function(){
  function fallback(id, msg){
    var el = document.getElementById(id);
    if (el) el.innerHTML = '<div style="padding:16px;color:#5f6368;font-size:13px;">' + msg + '</div>';
  }
  if (typeof echarts === 'undefined') {
    fallback('budgetChart','图表库未加载：预算对比——项目版 门票35/餐饮800/交通60/弹性89.5=984；标杆版 门票528/餐饮430/交通34/弹性99=1091。');
    fallback('funnelChart','图表库未加载：候选漏斗 20→12→9→4。');
    return;
  }
  try {
    var b = echarts.init(document.getElementById('budgetChart'));
    b.setOption({
      backgroundColor:'transparent',
      title:{text:'预算结构：项目版 vs 标杆版（元）',left:'center',textStyle:{fontSize:15,color:'#1A1B1C'}},
      tooltip:{trigger:'axis',triggerOn:'click',renderMode:'richText',confine:true,axisPointer:{type:'shadow'},textStyle:{fontSize:11}},
      legend:{top:30,itemWidth:14,itemHeight:9,textStyle:{fontSize:11,color:'#5f6368'}},
      grid:{left:48,right:20,top:66,bottom:30,containLabel:true},
      xAxis:{type:'category',data:['项目版（984）','标杆版（1091）'],axisLabel:{fontSize:12,color:'#333'}},
      yAxis:{type:'value',name:'元',axisLabel:{fontSize:11}},
      series:[
        {name:'门票',type:'bar',stack:'t',data:[35,528],itemStyle:{color:'#3E8FBF'},label:{show:true,fontSize:10,color:'#fff'}},
        {name:'餐饮',type:'bar',stack:'t',data:[800,430],itemStyle:{color:'#F4B393'},label:{show:true,fontSize:10,color:'#5a3a20'}},
        {name:'交通',type:'bar',stack:'t',data:[60,34],itemStyle:{color:'#94D8C3'},label:{show:true,fontSize:10}},
        {name:'弹性10%',type:'bar',stack:'t',data:[89.5,99.2],itemStyle:{color:'#c9d2d8'},label:{show:true,fontSize:10}}
      ]
    });
    var f = echarts.init(document.getElementById('funnelChart'));
    f.setOption({
      backgroundColor:'transparent',
      title:{text:'北京案例：候选到行程的转化漏斗',left:'center',textStyle:{fontSize:15,color:'#1A1B1C'}},
      tooltip:{trigger:'item',triggerOn:'click',renderMode:'richText',confine:true,textStyle:{fontSize:11},formatter:function(p){return p.name+'：'+p.value+' 个';}},
      series:[{
        type:'funnel',left:'12%',right:'12%',top:48,bottom:10,minSize:'38%',
        label:{show:true,position:'inside',fontSize:12,formatter:function(p){return p.name+'  '+p.value;}},
        data:[
          {value:20,name:'LLM 圈定候选',itemStyle:{color:'#9fc6dd'}},
          {value:12,name:'抖音验证采集',itemStyle:{color:'#7fb4d2'}},
          {value:9,name:'交叉验证保留（8景点+1餐厅）',itemStyle:{color:'#E8920C'}},
          {value:4,name:'最终排进行程（餐厅落位0）',itemStyle:{color:'#D9534F'}}
        ]
      }]
    });
    window.addEventListener('resize',function(){ try{b.resize();f.resize();}catch(e){} });
  } catch(e){ if(window&&window.console) console.error(e); }
})();
</script>
</body>
</html>
