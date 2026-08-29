"""抖音网页版采集器（浏览器自动化路线）。

只读取登录用户正常浏览时可见的内容：搜索结果 -> 视频页文案/标签/点赞 -> 评论区。
页面选择器全部集中在类顶部，平台改版时只需维护这里，不动业务逻辑。
"""
from __future__ import annotations

import random
import re
import time
from urllib.parse import quote

from DrissionPage import ChromiumPage

from config import MAX_COMMENTS_PER_VIDEO, MAX_SEARCH_SCROLLS
from core.models import Comment, VideoItem
from core.rate_limiter import RateLimiter
from core.sanitize import clean_comment, parse_comment_block, parse_count

VIDEO_ID_RE = re.compile(r"/video/(\d+)")
TAG_RE = re.compile(r"#([^#\s\[\]，。：:；;！!？?]+)")

# —— 选择器集中管理（候选列表按顺序尝试，失效时改这里）——
SEL_SEARCH_LINKS = 'css:a[href*="/video/"]'
SEL_VIDEO_DESC = ['css:[data-e2e="video-desc"]', "css:.video-info-detail", "css:h1"]
SEL_VIDEO_LIKE = ['css:[data-e2e="video-player-digg"]']
SEL_COMMENT_LIST = ['css:[data-e2e="comment-list"]', "css:.comment-mainContent"]
SEL_COMMENT_ITEM = ['css:[data-e2e="comment-item"]', "css:.comment-item"]


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


class DouyinCrawler:
    def __init__(self, page: ChromiumPage, limiter: RateLimiter | None = None):
        self.page = page
        self.limiter = limiter or RateLimiter()

    # ---- 搜索：返回视频 URL 列表 ----
    def search(self, keyword: str, limit: int) -> list[str]:
        url = f"https://www.douyin.com/search/{quote(keyword)}?type=video"
        self.page.get(url)
        time.sleep(4)

        found: list[str] = []
        seen: set[str] = set()
        for _ in range(MAX_SEARCH_SCROLLS):
            for link in self.page.eles(SEL_SEARCH_LINKS):
                href = link.attr("href") or ""
                m = VIDEO_ID_RE.search(href)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    found.append(f"https://www.douyin.com/video/{m.group(1)}")
                    if len(found) >= limit:
                        return found
            self.page.scroll.to_bottom()
            time.sleep(random.uniform(2.5, 4.0))
        return found

    # ---- 单个视频页：文案 + 标签 + 点赞 + 评论 ----
    def fetch_video(self, url: str, max_comments: int = MAX_COMMENTS_PER_VIDEO) -> VideoItem:
        self.limiter.wait()
        m = VIDEO_ID_RE.search(url)
        item = VideoItem(video_id=m.group(1) if m else url, url=url)
        self.page.get(url)
        time.sleep(4)

        desc = _first(self.page, SEL_VIDEO_DESC)
        if desc:
            item.description = desc.text.strip()
            item.tags = list(dict.fromkeys(TAG_RE.findall(item.description)))[:15]
        like = _first(self.page, SEL_VIDEO_LIKE)
        if like:
            item.like_count = parse_count(like.text)

        item.comments = self._fetch_comments(max_comments)
        return item

    def _fetch_comments(self, max_n: int) -> list[Comment]:
        """读取评论区：滚动加载 -> 结构化解析（昵称等个人信息在解析时即被丢弃）。"""
        container = _first(self.page, SEL_COMMENT_LIST, timeout=6)
        if container is None:
            return []

        collected: dict[str, Comment] = {}
        stale_rounds = 0
        while len(collected) < max_n and stale_rounds < 5:
            for node in _find_items(container, SEL_COMMENT_ITEM):
                text, like = parse_comment_block(node.text)
                if not text or text in collected:
                    continue
                collected[text] = clean_comment({"text": text[:500], "like_count": like})
                if len(collected) >= max_n:
                    break

            before = len(collected)
            self._scroll_comment_panel(container)
            time.sleep(random.uniform(1.5, 2.5))
            stale_rounds = stale_rounds + 1 if len(collected) == before else 0

        return sorted(collected.values(), key=lambda c: c.like_count or 0, reverse=True)

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
