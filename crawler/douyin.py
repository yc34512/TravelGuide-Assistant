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

from config import MAX_COMMENTS_PER_VIDEO, MAX_SEARCH_SCROLLS
from core.models import Comment, VideoItem
from core.rate_limiter import RateLimiter
from core.sanitize import clean_comment, parse_comment_block, parse_count, timestamp_to_date
from crawler.browser import block_heavy_resources

VIDEO_ID_RE = re.compile(r"/video/(\d+)")
TAG_RE = re.compile(r"#([^#\s\[\]，。：:；;！!？?]+)")
_CREATE_TIME_RE = re.compile(r'"create_time"\s*:\s*(\d{10})')

# —— 选择器集中管理（候选列表按顺序尝试，失效时改这里）——
SEL_SEARCH_LINKS = 'css:a[href*="/video/"]'
# 搜索结果卡片上的点赞数（尽力而为：解析失败退化为按原顺序采集，不阻断）
SEL_SEARCH_LIKE = ['css:[class*="like-count"]', 'css:[class*="digg"]', 'css:[class*="like"]']
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


def rank_candidates(candidates: list[dict], limit: int) -> list[str]:
    """择优排序：有点赞数的按点赞降序在前，无法解析的按发现顺序垫后；
    按 video_id 去重，取前 limit 条 URL。纯函数，独立可测。"""
    seen: set[str] = set()
    uniq: list[dict] = []
    for c in candidates:
        if c["video_id"] not in seen:
            seen.add(c["video_id"])
            uniq.append(c)
    scored = sorted(
        (c for c in uniq if c.get("like_count")),
        key=lambda c: c["like_count"],
        reverse=True,
    )
    unscored = [c for c in uniq if not c.get("like_count")]
    return [c["url"] for c in scored + unscored][:limit]


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


def author_uid_of(body: dict) -> str | None:
    """从视频详情包里取作者 UID（结构变动时返回 None，作者回复识别自动降级为不标）。"""
    detail = body.get("aweme_detail") or body.get("item_list") or body
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    author = (detail or {}).get("author") or {}
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
    def __init__(self, page: ChromiumPage, limiter: RateLimiter | None = None):
        self.page = page
        self.limiter = limiter or RateLimiter()
        self._blocked = None       # 重资源拦截状态：None 未设置 / False 拦了视频 / True 放行视频
        self._media_urls: list[str] = []   # 评论监听期间顺带抓到的媒体地址（ASR 用）

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
        url = f"https://www.douyin.com/search/{quote(keyword)}?type=video"
        self._apply_block(keep_video=False)
        self.limiter.wait()   # 搜索导航也是一次域名请求，同样过频控
        self.page.get(url)
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
            # 兜底：卡片文本末尾的独立计数（如 "1.2万"）
            m = re.search(r"([\d.]+\s*万?)\s*$", (link.text or "").strip())
            if m:
                return parse_count(m.group(1))
        except Exception:
            pass
        return None

    # ---- 搜索 + 择优：多角度查询合并候选池，按点赞排序后取前 limit 条 ----
    def search_and_rank(self, keyword: str, limit: int) -> list[str]:
        """先搜原词（候选池 2 倍）；候选池没饱和时再补一轮"避雷"角度查询。

        减量策略：第一轮已凑足候选池就跳过第二轮（省一次完整搜索+滚动，约 20~30 秒）；
        不足时第二轮滚动轮数减半——避雷素材仍要有，但不必全量翻页。
        扩展查询失败静默跳过。"""
        pool = self._search_one(keyword, limit * 2)
        if len(pool) < limit * 2:
            try:
                pool.extend(self._search_one(f"{keyword} 避雷", limit,
                                             scrolls=max(2, MAX_SEARCH_SCROLLS // 2)))
            except Exception:
                pass
        return rank_candidates(pool, limit)

    # ---- 单个视频页：文案 + 标签 + 点赞 + 发布时间 + (可选)口播转写 + 评论 ----
    def fetch_video(self, url: str, max_comments: int = MAX_COMMENTS_PER_VIDEO,
                    with_asr: bool = False, collect_comments: bool = True) -> VideoItem:
        """collect_comments=False 为元数据模式（热度刷榜用）：只取文案/点赞/发布时间，
        跳过评论滚动与解析，单条耗时降为完整采集的约三分之一。"""
        self._apply_block(keep_video=with_asr)
        self.limiter.wait()
        m = VIDEO_ID_RE.search(url)
        item = VideoItem(video_id=m.group(1) if m else url, url=url)
        self._media_urls = []

        # 监听必须在导航前开启：评论走接口 JSON（快且带精确时间戳），ASR 走视频 CDN 域
        targets = []
        if with_asr:
            targets.append(MEDIA_TARGET)
        if collect_comments:
            targets += [COMMENT_API_TARGET, AWEME_DETAIL_TARGET]
        if targets:
            try:
                self.page.listen.start(targets[0] if len(targets) == 1 else tuple(targets))
            except Exception:
                pass
        self.page.get(url)

        # 条件等待：文案元素出现即继续（替代固定 sleep 4 秒）
        desc = _wait_any(self.page, SEL_VIDEO_DESC, timeout=NAV_WAIT)
        if desc:
            item.description = desc.text.strip()
            item.tags = list(dict.fromkeys(TAG_RE.findall(item.description)))[:15]
        like = _wait_any(self.page, SEL_VIDEO_LIKE, timeout=2)
        if like:
            item.like_count = parse_count(like.text)

        item.publish_time = self._extract_publish_time()
        item.comments = self._fetch_comments(max_comments) if collect_comments else []

        if with_asr:
            # 只捕获媒体地址（音视频分离：需挑含音轨的），转写在采集结束后并行做；
            # 评论监听期间已顺带收到的就不重复等
            item.play_urls = self._media_urls or self._capture_play_urls()
        try:
            self.page.listen.stop()
        except Exception:
            pass
        return item

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
        """消费监听窗口内的数据包攒评论：滚评论面板触发翻页请求，包到即解析。

        返回脱敏前的字段字典列表；拿不到包时返回空列表，由调用方降级 DOM 路径。"""
        collected: dict[str, dict] = {}
        author_uid: str | None = None
        idle = 0
        deadline = time.time() + COMMENT_LISTEN_TIMEOUT
        while time.time() < deadline and len(collected) < max_n and idle < COMMENT_IDLE_ROUNDS:
            pk = self._wait_packet()
            if pk is None:
                idle += 1
                if container is not None:
                    self._scroll_comment_panel(container)   # 触发下一页评论请求
                continue
            url = getattr(pk, "url", "") or ""
            if MEDIA_TARGET in url:
                if url not in self._media_urls:
                    self._media_urls.append(url)
                continue
            body = packet_json(pk)
            if not body:
                continue
            if COMMENT_API_TARGET not in url:
                author_uid = author_uid or author_uid_of(body)   # 视频详情：取作者 UID
                continue
            before = len(collected)
            for row in parse_comment_payload(body.get("comments"), author_uid):
                collected.setdefault(row["text"], row)
            idle = 0 if len(collected) > before else idle + 1
        return list(collected.values())[:max_n]

    def _wait_packet(self, timeout: float = 2.0):
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
