"""多标签页并发采集。

"提速但不提高风控风险"的核心约束：Tab 只让页面渲染与等待并行，
每次导航前仍要经过全局共享的频控器（core.rate_limiter）预约槽位，
所以并发度提高不会缩短发往抖音域名的实际请求间隔——单位时间请求数与串行时一致。

线程安全约定：页面对象本身不是线程安全的，因此每个 Tab 配一把锁，
同一标签页任意时刻只有一个线程在驱动它；任务按序号轮转分配到各 Tab。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import CRAWL_TABS, MAX_COMMENTS_PER_VIDEO
from core.rate_limiter import global_limiter

FETCH_RETRIES = 2      # 单条视频采集失败自动重试次数（页面渲染/网络偶发抖动很常见）
_RETRY_GAP = 3         # 重试前等待秒数
_log_lock = threading.Lock()


def open_tabs(page, n: int) -> list:
    """开 n 个工作标签页（首个就是传入的主页面）。开不出来则退化为可用数量，至少 1 个。"""
    tabs = [page]
    for _ in range(max(0, n - 1)):
        try:
            tab = page.new_tab()
        except Exception:
            break
        if tab is None:
            break
        tabs.append(tab)
    return tabs


def close_tabs(tabs: list, keep) -> None:
    """关掉除 keep 之外的标签页（异常静默：浏览器退出时也会一并回收）。"""
    for t in tabs:
        if t is keep:
            continue
        try:
            t.close()
        except Exception:
            pass


def fetch_videos(page, urls, *, comments: int = MAX_COMMENTS_PER_VIDEO, asr: bool = False,
                 per_item_kwargs=None, workers: int | None = None, log=None,
                 cancelled=None, retries: int = FETCH_RETRIES, on_error=None) -> list:
    """多 Tab 并发采集视频，返回按原顺序的 [(序号, VideoItem 或 None, 异常或 None)]。

    per_item_kwargs(i, url) -> dict：逐条定制 fetch_video 参数（如只有首条采评论）；
    workers=1 时不额外开 Tab、在当前线程串行执行，行为等价于旧的串行循环；
    log/cancelled/on_error 由调用方注入（进度日志、任务取消、失败存快照）。
    """
    from crawler.douyin import DouyinCrawler

    total = len(urls)
    n_tabs = max(1, min(workers if workers else CRAWL_TABS, total or 1))
    tabs = open_tabs(page, n_tabs)
    limiter = global_limiter()          # 关键：所有 Tab 共享同一条请求时间轴
    crawlers = [DouyinCrawler(t, limiter) for t in tabs]
    locks = [threading.Lock() for _ in crawlers]
    out_items: list = [None] * total
    out_errs: list = [None] * total

    def _log(msg: str) -> None:
        if log:
            with _log_lock:
                log(msg)

    def work(i: int) -> None:
        url = urls[i]
        idx = i % len(crawlers)
        with locks[idx]:                # 一个 Tab 同时只被一个线程驱动
            tab = tabs[idx]
            last_err = None
            for attempt in range(retries + 1):
                if cancelled and cancelled():
                    return
                try:
                    kw = dict(per_item_kwargs(i, url)) if per_item_kwargs else {}
                    kw.setdefault("max_comments", comments)
                    kw.setdefault("with_asr", asr)
                    item = crawlers[idx].fetch_video(url, **kw)
                    out_items[i] = item
                    last_err = None
                    msg = (f"[{i + 1}/{total}] {item.video_id} | 文案 {len(item.description)} 字 | "
                           f"评论 {len(item.comments)} 条")
                    if asr and item.play_urls:
                        msg += f" | 已捕获 {len(item.play_urls)} 个媒体地址"
                    if attempt:
                        msg += f"（第 {attempt + 1} 次尝试成功）"
                    _log(msg)
                    break
                except Exception as e:
                    last_err = e
                    if attempt < retries:
                        _log(f"[{i + 1}/{total}] 采集失败：{e}，稍后重试…")
                        time.sleep(_RETRY_GAP)
            out_errs[i] = last_err
            if last_err is not None:
                _log(f"[{i + 1}/{total}] 采集失败（已重试 {retries} 次）：{last_err}")
                if on_error:
                    try:
                        on_error(i, last_err, tab)
                    except Exception:
                        pass

    try:
        if len(tabs) == 1:
            for i in range(total):
                if cancelled and cancelled():
                    break
                work(i)
        else:
            with ThreadPoolExecutor(max_workers=len(tabs)) as pool:
                list(pool.map(work, range(total)))
    finally:
        close_tabs(tabs, page)
    return [(i, out_items[i], out_errs[i]) for i in range(total)]
