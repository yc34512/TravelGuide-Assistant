"""抖音网页版采集器（浏览器自动化路线）。

只读取登录用户正常浏览时可见的内容：搜索结果 -> 视频页文案/标签/点赞 -> 评论区。
页面选择器全部集中在类顶部，平台改版时只需维护这里，不动业务逻辑。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from urllib.parse import quote

from DrissionPage import ChromiumPage

from config import (MAX_COMMENTS_PER_VIDEO, MAX_SEARCH_SCROLLS, SEARCH_POOL_SIZE,
                    SEARCH_SORT_BY_LIKES)
from core.models import Comment, VideoItem
from core.quality import screen_pool, video_quality_score
from core.rate_limiter import RateLimiter
from core.sanitize import clean_comment, parse_comment_block, parse_count, timestamp_to_date
from crawler import base
from crawler.browser import block_heavy_resources

VIDEO_ID_RE = re.compile(r"/video/(\d+)")
TAG_RE = re.compile(r"#([^#\s\[\]，。：:；;！!？?]+)")
_CREATE_TIME_RE = re.compile(r'"create_time"\s*:\s*(\d{10})')

# —— 选择器集中管理（候选列表按顺序尝试，失效时改这里）——
SEL_SEARCH_LINKS = 'css:a[href*="/video/"]'
# 搜索结果卡片上的点赞数（尽力而为：全部失效时 core.quality.screen_pool 会把筛选
# 延后到详情页逐条判定，不阻断采集、也不瞎猜点赞数）
SEL_SEARCH_LIKE = ['css:[class*="like-count"]', 'css:[class*="digg"]', 'css:[class*="like"]',
                   'css:[data-e2e*="like"]', 'css:[class*="count"]']
SEL_VIDEO_DESC = [
    'css:[data-e2e="video-desc"]',
    'css:[data-e2e="detail-video-info"]',
    "css:.video-info-detail",
    "css:h1",
]
SEL_VIDEO_LIKE = ['css:[data-e2e="video-player-digg"]']
SEL_VIDEO_PUBTIME = [
    'css:[data-e2e="detail-video-publish-time"]',
    'css:[data-e2e="video-create-time"]',
    "css:.create-time",
]
SEL_COMMENT_LIST = ['css:[data-e2e="comment-list"]', "css:.comment-mainContent"]
SEL_COMMENT_ITEM = ['css:[data-e2e="comment-item"]', "css:.comment-item"]

# 质量过滤：点赞低于门槛的评论信息量低（表情/跟风刷屏居多），采集后丢弃；
# 高赞评论不足保底数时放宽门槛，避免过冷门的视频被过滤到所剩无几。
MIN_COMMENT_LIKES = 2
MIN_COMMENTS_KEEP = 5

# —— 评论接口监听（比滚 DOM 快一个量级，且能拿到精确时间戳）——
COMMENT_API_TARGET = "comment/list"     # 一级评论与回复列表共用该路径片段
AWEME_DETAIL_TARGET = "aweme/detail"    # 视频详情：取作者 UID，用于识别作者回复
MEDIA_TARGET = "douyinvod"              # 视频 CDN：ASR 取播放地址
COMMENT_LISTEN_TIMEOUT = 25             # 单条视频评论监听的总时间预算（秒）
COMMENT_IDLE_ROUNDS = 3                 # 连续几轮滚动后仍无新包就收工
PACKET_WAIT = 3.0                       # 单次等包上限（实测翻页请求 2~3 秒到达）
DETAIL_WAIT = 3.0                       # 等视频详情包的上限（页面加载时就发出，通常不到 1 秒）
PACKET_BUF_MAX = 50                     # 详情环节暂存其他包的上限（防意外增长）

# —— 条件等待（替代写死的 sleep：命中即返回，未命中才等满上限）——
NAV_WAIT = 8.0             # 页面关键元素等待上限
SCROLL_WAIT = 3.0          # 搜索页滚动后等新卡片的等待上限
COMMENT_SCROLL_WAIT = 2.5  # 评论区滚动后等新条目的等待上限
POLL_INTERVAL = 0.25       # 轮询间隔


def _wait_any(scope, selectors: list[str], timeout: float = NAV_WAIT):
    """条件等待：轮询候选选择器，命中即返回，总耗时以 timeout 封顶。

    逐个选择器累加 timeout 的写法在多选择器场景最坏会等好几倍时长，
    这里用统一截止时间约束，等待上限可预期。"""
    deadline = time.time() + timeout
    while True:
        for sel in selectors:
            try:
                ele = scope.ele(sel, timeout=0.2)
            except Exception:
                ele = None
            if ele:
                return ele
        if time.time() >= deadline:
            return None
        time.sleep(POLL_INTERVAL)


def _wait_until(cond, timeout: float, interval: float = 0.4) -> bool:
    """条件等待：轮询 cond() 直到为真或超时，返回是否命中（替代固定 sleep）。"""
    deadline = time.time() + timeout
    while True:
        try:
            if cond():
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def _find_items(container, selectors: list[str]):
    for sel in selectors:
        found = container.eles(sel)
        if found:
            return found
    return []


# 搜索地址的排序参数。sort_type: 0 综合排序 / 1 最多点赞 / 2 最新发布。
# 尽力而为：平台不认这个参数时会退回综合排序，两种情况都由本地质量闸兜底。
_SORT_PARAM = "&sort_type=1" if SEARCH_SORT_BY_LIKES else ""


def search_url(keyword: str) -> str:
    """搜索结果页地址（纯函数，独立可测）。默认带"最多点赞"排序。"""
    return f"https://www.douyin.com/search/{quote(keyword)}?type=video{_SORT_PARAM}"


def _stat_int(v) -> int | None:
    """详情接口计数字段归一：int 直接用，字符串走 parse_count，其余 None（不猜数）。"""
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str):
        return parse_count(v)
    return None


def _stat_duration(v) -> float | None:
    """视频时长归一到秒。抖音详情接口的 duration 多为毫秒，个别字段给秒，
    按数量级判别；拿不准返回 None（质量闸对 None 不生效，宁缺勿错）。"""
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return round(d / 1000.0, 1) if d >= 1000 else round(d, 1)


def dedupe_pool(candidates: list[dict]) -> list[dict]:
    """候选池按 video_id 去重，保留首次发现顺序（多查询词合并用）。纯函数可测。"""
    seen: set[str] = set()
    out: list[dict] = []
    for c in candidates or []:
        vid = c.get("video_id")
        if vid and vid not in seen:
            seen.add(vid)
            out.append(c)
    return out


def rank_pool_dicts(pool: list[dict]) -> list[dict]:
    """按质量分降序稳定排序（不淘汰、不降档——门槛判定归 core.quality.screen_pool）。

    搜索页候选只有点赞数时，质量分是点赞的单调函数（收藏/评论维度为 0、
    新鲜度取中性值），因此排序结果与旧的"点赞降序 + 无点赞垫后"完全一致，
    旧调用方与旧测试行为不变。纯函数，独立可测。"""
    return sorted(dedupe_pool(pool), key=video_quality_score, reverse=True)


def rank_candidates(candidates: list[dict], limit: int) -> list[str]:
    """择优排序后取前 limit 条 URL（向后兼容出口，不做门槛过滤）。

    需要"门槛筛选 + 自动降档 + 淘汰明细"请用 DouyinCrawler.rank_pool。"""
    return [c["url"] for c in rank_pool_dicts(candidates)][:limit]


def parse_comment_payload(comments, author_uid: str | None = None) -> list[dict]:
    """评论接口 JSON 条目 -> 清洗前的字段字典列表（纯函数可测）。

    只保留正文/点赞/精确日期/是否作者回复四项；昵称、UID、头像、IP 属地等
    个人信息在这里就被丢弃（作者 UID 只参与比对，不写进结果），
    与 DOM 路径共用 clean_comment 白名单这唯一合规出口。"""
    out = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        uid = str((c.get("user") or {}).get("uid") or "")
        like = c.get("digg_count")
        out.append({
            "text": text[:500],
            "like_count": like if isinstance(like, int) else parse_count(like),
            "is_author_reply": bool(author_uid and uid and uid == str(author_uid)),
            "time": timestamp_to_date(c.get("create_time")),
        })
    return out


def packet_json(pk) -> dict | None:
    """取出监听包的 JSON 响应体；非 JSON 或解析失败返回 None（静默降级）。"""
    try:
        body = pk.response.body
    except Exception:
        return None
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, str)):
        try:
            data = json.loads(body)
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    return None


def detail_node(body: dict) -> dict:
    """从视频详情包里取出 aweme 节点（结构变动时返回空 dict）。纯函数可测。"""
    if not isinstance(body, dict):
        return {}
    node = body.get("aweme_detail") or body.get("item_list") or body.get("aweme") or body
    if isinstance(node, list):
        node = node[0] if node else {}
    return node if isinstance(node, dict) else {}


def author_uid_of(body: dict) -> str | None:
    """从视频详情（整包或已取出的 aweme 节点）里取作者 UID；
    结构变动时返回 None，作者回复识别自动降级为不标。"""
    author = detail_node(body).get("author") or {}
    uid = author.get("uid") or author.get("sec_uid")
    return str(uid) if uid else None


def rank_and_filter(comments: list[Comment], max_n: int) -> list[Comment]:
    """点赞排序 + 低质过滤（DOM 与 JSON 两条采集路径共用，纯函数可测）。

    高赞评论更可能含真实经验；作者回复（博主亲自下场，常含权威澄清）豁免点赞门槛；
    过滤后不足保底数时放宽门槛，保证条数。"""
    ranked = sorted(comments, key=lambda c: c.like_count or 0, reverse=True)
    keep = [c for c in ranked if c.is_author_reply or (c.like_count or 0) >= MIN_COMMENT_LIKES]
    if len(keep) < MIN_COMMENTS_KEEP:
        keep = ranked
    return keep[:max_n]


class DouyinCrawler:
    # 详情接口质量字段探测：每进程只打印一次（实跑确认口径用，避免刷屏）
    _stats_probed = False

    def __init__(self, page: ChromiumPage, limiter: RateLimiter | None = None):
        self.page = page
        self.limiter = limiter or RateLimiter()
        self._blocked = None       # 重资源拦截状态：None 未设置 / False 拦了视频 / True 放行视频
        self._media_urls: list[str] = []   # 评论监听期间顺带抓到的媒体地址（ASR 用）
        self._pkt_buf: list = []           # 详情环节暂存的评论/媒体包（不丢包）
        self._author_uid: str | None = None

    def _apply_block(self, keep_video: bool) -> None:
        """每个标签页只设置一次重资源拦截；ASR 需要视频时重设为放行。"""
        if self._blocked is not None and (self._blocked or not keep_video):
            return
        try:
            block_heavy_resources(self.page, keep_video=keep_video)
        finally:
            self._blocked = keep_video

    # ---- 搜索：单次查询，返回候选列表（带尽力解析的点赞数）----
    def _search_one(self, keyword: str, max_n: int, scrolls: int | None = None) -> list[dict]:
        """搜索结果页采集。scrolls 可指定滚动轮数（补充查询用更少轮数省时间）。"""
        if base.session_stopped():
            return []   # 本会话已触发验证码风控：不再发新搜索请求（避免加重风控）
        url = search_url(keyword)
        self._apply_block(keep_video=False)
        self.limiter.wait()   # 搜索导航也是一次域名请求，同样过频控
        self.page.get(url)
        if base.captcha_detected(self.page):
            base.stop_session()   # 只停止、不绕过：后续候选回退缓存/LLM 基线
            return []
        # 条件等待：结果卡片出现即开始收集（替代固定 sleep 4 秒）；没渲染出来直接返回
        if _wait_any(self.page, [SEL_SEARCH_LINKS], timeout=NAV_WAIT) is None:
            return []

        found: list[dict] = []
        seen: set[str] = set()
        for _ in range(MAX_SEARCH_SCROLLS if scrolls is None else scrolls):
            before = self._harvest_links(found, seen, max_n)
            if before >= max_n:
                return found
            self.page.scroll.to_bottom()
            # 条件等待：新卡片出现就进下一轮（替代固定 2.5~4 秒）
            _wait_until(lambda: self._harvest_links(found, seen, max_n) > before, SCROLL_WAIT)
        return found

    def _harvest_links(self, found: list, seen: set, max_n: int) -> int:
        """把当前页面上的视频卡片收进候选池（按 video_id 去重），返回池子大小。"""
        for link in self.page.eles(SEL_SEARCH_LINKS):
            href = link.attr("href") or ""
            m = VIDEO_ID_RE.search(href)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                found.append(
                    {
                        "video_id": m.group(1),
                        "url": f"https://www.douyin.com/video/{m.group(1)}",
                        "like_count": self._card_like_count(link),
                    }
                )
                if len(found) >= max_n:
                    break
        return len(found)

    def _card_like_count(self, link) -> int | None:
        """尽力解析搜索结果卡片点赞数：任何异常/未命中都返回 None（
        排序退化为按发现顺序），不阻断采集。"""
        try:
            for sel in SEL_SEARCH_LIKE:
                try:
                    ele = link.ele(sel, timeout=0.2)
                except Exception:
                    ele = None
                if ele:
                    n = parse_count(ele.text)
                    if n is not None:
                        return n
            text = (link.text or "").strip()
            # 兜底 1：卡片文本末尾的独立计数（如 "1.2万"）
            m = re.search(r"([\d.]+\s*万?)\s*$", text)
            if m:
                n = parse_count(m.group(1))
                if n:
                    return n
            # 兜底 2：全文中最后一个带"万"的计数（点赞数在卡片右下角，通常是末位计数）。
            # 选择器失效时靠这个把"按点赞择优"救回来，避免排序整体退化成综合排序原样
            hits = re.findall(r"[\d.]+\s*万", text)
            if hits:
                return parse_count(hits[-1])
        except Exception:
            pass
        return None

    # ---- 搜索：多角度扩池 + 质量闸择优 ----
    def search_pool(self, queries: list[str], pool_size: int = SEARCH_POOL_SIZE,
                    scrolls: int | None = None) -> list[dict]:
        """多查询词合并候选池（按 video_id 去重，保留首次发现顺序）。

        为什么多角度扩池是划算的：每个查询词是一次页面导航，而滚动加载更多卡片
        不产生新的域名请求——"1 次导航收 30 个候选"的风控成本远低于"多采 25 个详情页"
        （后者会撞会话级验证码）。池子收满即停，不再发多余导航。
        单个查询词失败静默跳过（其余仍可用）；本会话已触发风控则不再发新搜索。"""
        base.require_ugc_source()   # 开源合规闸门：UGC 源默认关闭
        pool: list[dict] = []
        seen: set[str] = set()
        for i, q in enumerate(dict.fromkeys(x for x in (queries or []) if str(x).strip())):
            if base.session_stopped():
                break
            want = pool_size - len(pool)
            if want <= 0:
                break
            # 首个查询词全量滚动，后续补充角度滚动减半（素材要有，不必全量翻页）
            sc = scrolls if scrolls is not None else (
                MAX_SEARCH_SCROLLS if i == 0 else max(2, MAX_SEARCH_SCROLLS // 2))
            try:
                got = self._search_one(q, want, scrolls=sc)
            except Exception:
                continue
            for c in got:
                if c["video_id"] not in seen:
                    seen.add(c["video_id"])
                    pool.append(c)
        return pool

    def rank_pool(self, pool: list[dict], limit: int, level: str | None = None) -> dict:
        """候选池 → 质量闸筛选结果（core.quality.screen_pool 的采集层入口）。

        返回 {kept, picked, urls, level, relaxed, reasons, pool_size}。
        urls / picked 是首批深采名单（limit 条），kept 是全部达标候选（含候补队列），
        交给 crawler.tabs.fetch_videos_gated 分批消费——首批就达标时只导航 limit 次。
        level="deferred" 表示搜索页拿不到点赞，筛选已延后到详情页逐条判定
        （不猜数、不静默降标准）。"""
        res = screen_pool(pool, limit, level=level)
        res["urls"] = [c["url"] for c in res.get("picked") or []]
        return res

    def search_and_rank(self, keyword: str, limit: int, level: str | None = None) -> list[str]:
        """搜索 + 质量闸择优，返回要深采的视频地址（向后兼容旧签名）。

        查询角度：原词 → "{原词} 攻略" → "{原词} 避雷"（池子够大就不发多余导航）。
        需要筛选明细（淘汰了多少、降档到哪档）时改用 rank_pool。"""
        pool = self.search_pool([keyword, f"{keyword} 攻略", f"{keyword} 避雷"],
                                pool_size=max(SEARCH_POOL_SIZE, limit * 3))
        return self.rank_pool(pool, limit, level=level)["urls"]

    # ---- 单个视频页：文案 + 标签 + 点赞 + 发布时间 + (可选)口播转写 + 评论 ----
    def fetch_video(self, url: str, max_comments: int = MAX_COMMENTS_PER_VIDEO,
                    with_asr: bool = False, collect_comments: bool = True) -> VideoItem:
        """collect_comments=False 为元数据模式（热度刷榜用）：只取文案/点赞/发布时间，
        跳过评论滚动与解析，单条耗时降为完整采集的约三分之一。

        元数据优先取页面自身发出的详情接口 JSON，DOM 只作降级：多标签并发时
        后台页渲染会被浏览器节流，等 DOM 常常取不到文案（实测约 1/3 视频文案为空）。"""
        self._apply_block(keep_video=with_asr)
        self.limiter.wait()
        m = VIDEO_ID_RE.search(url)
        item = VideoItem(video_id=m.group(1) if m else url, url=url)
        self._media_urls = []
        self._pkt_buf = []
        self._author_uid = None

        # 监听必须在导航前开启：详情包给元数据，评论包给评论，ASR 另需视频 CDN 域
        targets = [AWEME_DETAIL_TARGET]
        if with_asr:
            targets.append(MEDIA_TARGET)
        if collect_comments:
            targets.append(COMMENT_API_TARGET)
        try:
            self.page.listen.start(targets[0] if len(targets) == 1 else tuple(targets))
        except Exception:
            pass
        self.page.get(url)

        node = detail_node(self._drain_detail())
        self._author_uid = author_uid_of(node) if node else None

        # 文案/点赞/发布时间：JSON 有就用，缺哪项才去等 DOM（条件等待，命中即返回）
        item.description = str(node.get("desc") or "").strip()
        if not item.description:
            desc = _wait_any(self.page, SEL_VIDEO_DESC, timeout=NAV_WAIT)
            if desc:
                item.description = (desc.text or "").strip()
        if item.description:
            item.tags = list(dict.fromkeys(TAG_RE.findall(item.description)))[:15]

        stats = node.get("statistics") or {}
        digg = stats.get("digg_count")
        if isinstance(digg, int) and digg > 0:
            item.like_count = digg
        else:
            like = _wait_any(self.page, SEL_VIDEO_LIKE, timeout=2)
            if like:
                item.like_count = parse_count(like.text)
        # 质量指标读全（M6）：以前只读 digg_count，播放量/收藏/分享/评论数/时长白白丢弃
        # （comment_count 字段甚至定义了却从未赋值），质量闸与热度计算都缺原料
        item.play_count = _stat_int(stats.get("play_count"))
        item.comment_count = _stat_int(stats.get("comment_count"))
        item.collect_count = _stat_int(stats.get("collect_count"))
        item.share_count = _stat_int(stats.get("share_count"))
        item.duration = _stat_duration(node.get("duration"))
        self._probe_stats_once(stats, node)

        item.publish_time = timestamp_to_date(node.get("create_time")) or self._extract_publish_time()
        item.comments = self._fetch_comments(max_comments) if collect_comments else []
        # 采回即打分：候补队列与排序直接用，调用方不必再算一遍
        item.quality_score = video_quality_score(item)

        if with_asr:
            # 只捕获媒体地址（音视频分离：需挑含音轨的），转写在采集结束后并行做；
            # 评论监听期间已顺带收到的就不重复等
            item.play_urls = self._media_urls or self._capture_play_urls()
        try:
            self.page.listen.stop()
        except Exception:
            pass
        return item

    def _probe_stats_once(self, stats: dict, node: dict) -> None:
        """首次采集时如实打印详情接口的可用质量字段（每进程一次）。

        实跑已确认 statistics 下发 play_count/collect_count/share_count/comment_count 与
        节点级 duration；保留这个探测是因为平台改版时字段可能消失，届时日志会直接
        告知哪个维度没了（字段缺失时质量闸自动对该维度不生效，不阻断采集）。"""
        if DouyinCrawler._stats_probed:
            return
        DouyinCrawler._stats_probed = True
        try:
            keys = sorted(k for k, v in (stats or {}).items() if isinstance(v, (int, str)))
            extra = [k for k in ("duration", "create_time", "aweme_type") if k in (node or {})]
            print(f"[质量闸探测] statistics 可用字段：{keys or '（空）'}；节点级：{extra or '（空）'}"
                  f"｜播放量 play_count：{'有' if 'play_count' in (stats or {}) else '无（该维度自动不加分，不猜数）'}")
        except Exception:
            pass

    def _drain_detail(self, timeout: float = DETAIL_WAIT) -> dict:
        """在监听包里找视频详情包，返回其 JSON 体（找不到给空 dict）。

        详情包由页面自身在加载时发出，比等 DOM 渲染可靠；期间到达的评论/媒体包
        暂存进缓冲区交给后续环节，不丢包。

        注意这里只从监听器取新包（不走缓冲区），否则会把刚暂存的包又取回来原地空转。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pk = self._wait_packet(timeout=0.6)
            if pk is None:
                continue
            body = packet_json(pk)
            if AWEME_DETAIL_TARGET in (getattr(pk, "url", "") or "") and body:
                return body
            self._pkt_buf.append(pk)
            if len(self._pkt_buf) >= PACKET_BUF_MAX:
                break
        return {}

    def _next_packet(self, timeout: float = PACKET_WAIT):
        """取下一个监听包：先消费缓冲区（详情环节暂存的），再等新的。"""
        if self._pkt_buf:
            return self._pkt_buf.pop(0)
        return self._wait_packet(timeout=timeout)

    def _capture_play_urls(self) -> list[str]:
        """收集监听窗口内所有视频 CDN 地址（去重）。抖音是音视频分离流，
        纯视频/纯音频各一个地址，转写要用含音轨的那个，由 asr 层自动挑选。"""
        try:
            p = self.page.listen.wait(count=4, timeout=20)
            packets = p if isinstance(p, list) else ([p] if p else [])
            urls: list[str] = []
            for pk in packets:
                u = getattr(pk, "url", "") or ""
                if MEDIA_TARGET in u and u not in urls:
                    urls.append(u)
            return urls
        except Exception:
            return []

    def _extract_publish_time(self) -> str | None:
        """视频发布日期，YYYY-MM-DD。双通道：DOM 文本 -> 页面内嵌状态 JSON。

        时效过滤的依据：JSON 里的 create_time 是 unix 秒级时间戳，取最小值——
        视频本体一定早于页面上其他带时间戳的对象。
        """
        ele = _wait_any(self.page, SEL_VIDEO_PUBTIME, timeout=2)
        if ele:
            m = re.search(r"20\d{2}-\d{2}-\d{2}", (ele.text or "").strip())
            if m:
                return m.group(0)
        try:
            stamps = [
                int(t)
                for t in _CREATE_TIME_RE.findall(self.page.html)
                if 1467000000 <= int(t) <= time.time()  # 抖音上线(2016年)至今为合理区间
            ]
        except Exception:
            return None
        if stamps:
            return datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%d")
        return None

    # ---- 评论：接口 JSON 优先，DOM 滚动解析降级 ----
    def _fetch_comments(self, max_n: int) -> list[Comment]:
        """读取评论区（昵称等个人信息两条路径都在解析时即被丢弃）。

        优先监听接口 JSON：不用逐轮重解析整棵 DOM，且拿到的是精确时间戳；
        监听不可用或接口改版时降级到滚动 DOM 解析，输出结构完全一致。"""
        container = _wait_any(self.page, SEL_COMMENT_LIST, timeout=6)
        rows = self._comments_by_listen(max_n, container)
        if rows:
            return rank_and_filter([clean_comment(r) for r in rows], max_n)
        if container is None:
            return []
        return rank_and_filter(self._comments_by_dom(max_n, container), max_n)

    def _comments_by_listen(self, max_n: int, container) -> list[dict]:
        """消费监听窗口内的数据包攒评论：每轮都滚一次评论面板触发翻页请求，包到即解析。

        翻页请求靠滚动触发，所以不能"只在等不到包时才滚"（那样拿到首页就卡死）；
        终止条件：够了 / 接口告知 has_more=0 / 连续空转几轮 / 超时间预算。
        返回脱敏前的字段字典列表；拿不到包时返回空列表，由调用方降级 DOM 路径。"""
        collected: dict[str, dict] = {}
        author_uid: str | None = self._author_uid   # 详情环节已拿到，作者回复识别不漏
        idle = 0
        has_more = True
        deadline = time.time() + COMMENT_LISTEN_TIMEOUT
        while (time.time() < deadline and len(collected) < max_n
               and idle < COMMENT_IDLE_ROUNDS and has_more):
            pk = self._next_packet(timeout=PACKET_WAIT)
            if pk is None:
                idle += 1
            else:
                url = getattr(pk, "url", "") or ""
                if MEDIA_TARGET in url:
                    if url not in self._media_urls:
                        self._media_urls.append(url)
                else:
                    body = packet_json(pk)
                    if body:
                        if COMMENT_API_TARGET not in url:
                            author_uid = author_uid or author_uid_of(body)   # 视频详情：取作者 UID
                        else:
                            before = len(collected)
                            for row in parse_comment_payload(body.get("comments"), author_uid):
                                collected.setdefault(row["text"], row)
                            has_more = bool(body.get("has_more", 0))
                            idle = 0 if len(collected) > before else idle + 1
            # 每轮都滚一下：翻页请求靠滚动触发，且滚动本身不产生额外域名请求
            if container is not None and len(collected) < max_n and has_more:
                self._scroll_comment_panel(container)
        return list(collected.values())[:max_n]

    def _wait_packet(self, timeout: float = PACKET_WAIT):
        """取一个监听包；超时或监听未开启返回 None（不抛异常，由调用方决定降级）。"""
        try:
            return self.page.listen.wait(count=1, timeout=timeout, fit_count=False, raise_err=False)
        except Exception:
            return None

    def _comments_by_dom(self, max_n: int, container) -> list[Comment]:
        """降级路径：滚动评论区解析条目 innerText（监听不可用/接口改版时兜底）。"""
        collected: dict[str, Comment] = {}
        stale_rounds = 0
        while len(collected) < max_n and stale_rounds < 5:
            before = self._harvest_dom_comments(container, collected, max_n)
            if before >= max_n:
                break
            self._scroll_comment_panel(container)
            # 条件等待：新评论入池即继续（替代固定 1.5~2.5 秒）
            _wait_until(lambda: self._harvest_dom_comments(container, collected, max_n) > before,
                        COMMENT_SCROLL_WAIT)
            stale_rounds = stale_rounds + 1 if len(collected) == before else 0
        return list(collected.values())

    def _harvest_dom_comments(self, container, collected: dict, max_n: int) -> int:
        """把当前评论区 DOM 条目解析进 collected（按正文去重），返回已收条数。"""
        for node in _find_items(container, SEL_COMMENT_ITEM):
            text, like, is_author, c_time = parse_comment_block(node.text)
            if not text or text in collected:
                continue
            collected[text] = clean_comment(
                {"text": text[:500], "like_count": like, "is_author_reply": is_author, "time": c_time}
            )
            if len(collected) >= max_n:
                break
        return len(collected)

    def _scroll_comment_panel(self, container) -> None:
        """评论面板是独立滚动容器：向上找到真正可滚动的祖先元素滚到底，
        同时派发滚轮事件兜底，触发虚拟列表的懒加载。"""
        js = """
        let el = arguments[0];
        let node = el, scroller = null;
        for (let i = 0; i < 8 && node; i++) {
            if (node.scrollHeight > node.clientHeight + 80) { scroller = node; break; }
            node = node.parentElement;
        }
        if (scroller) { scroller.scrollTop = scroller.scrollHeight; }
        el.dispatchEvent(new WheelEvent('wheel', {deltaY: 1500, bubbles: true}));
        """
        try:
            self.page.run_js(js, container)
        except Exception:
            try:
                container.run_js("this.scrollTop = this.scrollHeight")
            except Exception:
                self.page.scroll.to_bottom()
