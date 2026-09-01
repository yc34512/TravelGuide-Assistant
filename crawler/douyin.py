"""抖音网页版采集器（浏览器自动化路线）。

只读取登录用户正常浏览时可见的内容：搜索结果 -> 视频页文案/标签/点赞 -> 评论区。
页面选择器全部集中在类顶部，平台改版时只需维护这里，不动业务逻辑。
"""
from __future__ import annotations

import random
import re
import time
from datetime import datetime
from urllib.parse import quote

from DrissionPage import ChromiumPage

from config import MAX_COMMENTS_PER_VIDEO, MAX_SEARCH_SCROLLS
from core.models import Comment, VideoItem
from core.rate_limiter import RateLimiter
from core.sanitize import clean_comment, parse_comment_block, parse_count

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


def _first(scope, selectors: list[str], timeout: int = 3):
    """在候选选择器里找到第一个命中的元素。"""
    for sel in selectors:
        ele = scope.ele(sel, timeout=timeout)
        if ele:
            return ele
    return None


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


class DouyinCrawler:
    def __init__(self, page: ChromiumPage, limiter: RateLimiter | None = None):
        self.page = page
        self.limiter = limiter or RateLimiter()

    # ---- 搜索：单次查询，返回候选列表（带尽力解析的点赞数）----
    def _search_one(self, keyword: str, max_n: int) -> list[dict]:
        url = f"https://www.douyin.com/search/{quote(keyword)}?type=video"
        self.page.get(url)
        time.sleep(4)

        found: list[dict] = []
        seen: set[str] = set()
        for _ in range(MAX_SEARCH_SCROLLS):
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
                        return found
            self.page.scroll.to_bottom()
            time.sleep(random.uniform(2.5, 4.0))
        return found

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
        """先搜原词（候选池 2 倍），再补一轮"避雷"角度查询扩充避雷类素材；
        两条查询间走频控，扩展查询失败静默跳过。"""
        pool = self._search_one(keyword, limit * 2)
        try:
            self.limiter.wait()
            pool.extend(self._search_one(f"{keyword} 避雷", limit))
        except Exception:
            pass
        return rank_candidates(pool, limit)

    # ---- 单个视频页：文案 + 标签 + 点赞 + 发布时间 + (可选)口播转写 + 评论 ----
    def fetch_video(self, url: str, max_comments: int = MAX_COMMENTS_PER_VIDEO,
                    with_asr: bool = False, collect_comments: bool = True) -> VideoItem:
        """collect_comments=False 为元数据模式（热度刷榜用）：只取文案/点赞/发布时间，
        跳过评论滚动与解析，单条耗时降为完整采集的约三分之一。"""
        self.limiter.wait()
        m = VIDEO_ID_RE.search(url)
        item = VideoItem(video_id=m.group(1) if m else url, url=url)

        if with_asr:
            # 监听必须在导航前开启；douyinvod 是抖音视频 CDN 专用域
            self.page.listen.start("douyinvod")
        self.page.get(url)
        time.sleep(4)

        desc = _first(self.page, SEL_VIDEO_DESC)
        if desc:
            item.description = desc.text.strip()
            item.tags = list(dict.fromkeys(TAG_RE.findall(item.description)))[:15]
        like = _first(self.page, SEL_VIDEO_LIKE)
        if like:
            item.like_count = parse_count(like.text)

        item.publish_time = self._extract_publish_time()

        if with_asr:
            # 只捕获媒体地址（音视频分离：需挑含音轨的），转写在采集结束后并行做
            item.play_urls = self._capture_play_urls()
            self.page.listen.stop()

        item.comments = self._fetch_comments(max_comments) if collect_comments else []
        return item

    def _capture_play_urls(self) -> list[str]:
        """收集监听窗口内所有 douyinvod 地址（去重）。抖音是音视频分离流，
        纯视频/纯音频各一个地址，转写要用含音轨的那个，由 asr 层自动挑选。"""
        try:
            p = self.page.listen.wait(count=4, timeout=20)
            packets = p if isinstance(p, list) else ([p] if p else [])
            urls: list[str] = []
            for pk in packets:
                u = getattr(pk, "url", "") or ""
                if "douyinvod" in u and u not in urls:
                    urls.append(u)
            return urls
        except Exception:
            return []

    def _extract_publish_time(self) -> str | None:
        """视频发布日期，YYYY-MM-DD。双通道：DOM 文本 -> 页面内嵌状态 JSON。

        时效过滤的依据：JSON 里的 create_time 是 unix 秒级时间戳，取最小值——
        视频本体一定早于页面上其他带时间戳的对象。
        """
        for sel in SEL_VIDEO_PUBTIME:
            ele = self.page.ele(sel, timeout=2)
            if ele:
                text = ele.text.strip()
                m = re.search(r"20\d{2}-\d{2}-\d{2}", text)
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

    def _fetch_comments(self, max_n: int) -> list[Comment]:
        """读取评论区：滚动加载 -> 结构化解析（昵称等个人信息在解析时即被丢弃）。"""
        container = _first(self.page, SEL_COMMENT_LIST, timeout=6)
        if container is None:
            return []

        collected: dict[str, Comment] = {}
        stale_rounds = 0
        while len(collected) < max_n and stale_rounds < 5:
            for node in _find_items(container, SEL_COMMENT_ITEM):
                text, like, is_author = parse_comment_block(node.text)
                if not text or text in collected:
                    continue
                collected[text] = clean_comment(
                    {"text": text[:500], "like_count": like, "is_author_reply": is_author}
                )
                if len(collected) >= max_n:
                    break

            before = len(collected)
            self._scroll_comment_panel(container)
            time.sleep(random.uniform(1.5, 2.5))
            stale_rounds = stale_rounds + 1 if len(collected) == before else 0

        # 点赞排序 + 低质过滤：高赞评论更可能含真实经验；作者回复（博主亲自下场，
        # 常含权威澄清）豁免点赞门槛；不足保底数时放宽，保证条数
        ranked = sorted(collected.values(), key=lambda c: c.like_count or 0, reverse=True)
        keep = [
            c for c in ranked
            if c.is_author_reply or (c.like_count or 0) >= MIN_COMMENT_LIKES
        ]
        if len(keep) < MIN_COMMENTS_KEEP:
            keep = ranked
        return keep[:max_n]

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
