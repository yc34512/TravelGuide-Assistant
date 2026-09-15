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

from config import (CRAWL_TABS, MAX_COMMENTS_PER_VIDEO, MAX_DETAIL_FETCH,
                    MIN_KEEP_BEFORE_RELAX, QUALITY_LEVEL)
from core.rate_limiter import global_limiter
from core.quality import gate_and_backfill, passes_gate, relax_level
from crawler import base

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
    base.require_ugc_source()   # 开源合规闸门：UGC 源默认关闭，任何多 Tab 采集都先过闸

    if base.session_stopped():
        # 本会话已触发验证码风控：一条也不采（不新开 Tab、不发请求），按"未采到"返回
        if log:
            log("本会话已触发验证码风控：跳过详情采集（不再发新请求，不尝试绕过）")
        err = RuntimeError("验证码风控：本会话停止现采")
        return [(i, None, err) for i in range(len(urls))]

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
                if base.session_stopped():
                    # 风控已触发：本条不再重试（重试只会继续撞验证码）
                    out_errs[i] = RuntimeError("验证码风控：本会话停止现采")
                    _log(f"[{i + 1}/{total}] 跳过：本会话已触发验证码风控")
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


def fetch_videos_gated(page, candidates, target_n: int, *,
                       comments: int = MAX_COMMENTS_PER_VIDEO, asr: bool = False,
                       level: str | None = None, workers: int | None = None,
                       log=None, cancelled=None, on_error=None,
                       max_fetch: int | None = None) -> dict:
    """质量闸驱动的分批候补补采（核心）：深采一批 → 校验 → 不达标的从候补顶上。

    为何分批而不是一次全采：搜索页能解析到点赞时，质量筛选已在那里完成，
    这里第一批就达标，详情页导航次数与从前完全相同（最好情况零额外成本）；
    只有搜索页信息不足（level=deferred）时才靠候补逐条筛。

    风控红线：max_fetch（默认 config.MAX_DETAIL_FETCH）是详情页导航硬上限。抖音是
    会话级风控，连续 5~6 次视频页导航后弹 3D 验证码、之后整个会话返回 0 结果，
    所以宁可少采也不越界。达标不足时只对**已采回**的数据降档重判（零额外导航），
    不会为了凑数去撞风控。

    candidates：已按质量分排序的候选字典列表（DouyinCrawler.rank_pool 的 kept）。
    返回 {items, rejected, reasons, fetched, capped, exhausted, relaxed, level, note}。
    """
    base.require_ugc_source()   # 开源合规闸门：UGC 源默认关闭
    max_fetch = max_fetch or MAX_DETAIL_FETCH
    cands = [c for c in (candidates or []) if c.get("url")]
    target_n = max(1, int(target_n or 1))
    # 无候选可采：搜索阶段就没结果（验证码风控/关键词过冷/选择器失效）。
    # 如实说明原因，不报成"门槛过严"也不报成"候选池用尽"（两者都会带偏排查方向）
    if not cands:
        return {"items": [], "rejected": [], "reasons": {}, "fetched": 0,
                "capped": False, "exhausted": False, "relaxed": [],
                "level": level if level else QUALITY_LEVEL,
                "note": "无候选可采：搜索阶段未返回结果（验证码风控/关键词过冷/选择器失效），"
                        "不是门槛过严，本次未消耗任何详情页导航额度"}
    kept: list = []
    rejected: list = []
    reasons: dict = {}
    cursor = fetched = 0
    capped = exhausted = False
    lv = level if level else QUALITY_LEVEL

    while len(kept) < target_n:
        if cancelled and cancelled():
            break
        room = max_fetch - fetched
        if room <= 0:
            capped = True
            break
        if cursor >= len(cands):
            exhausted = True
            break
        batch = cands[cursor:cursor + min(target_n - len(kept), room)]
        cursor += len(batch)
        urls = [c["url"] for c in batch]
        fetched += len(urls)
        got = []
        for _i, item, _err in fetch_videos(page, urls, comments=comments, asr=asr,
                                          workers=workers, log=log, cancelled=cancelled,
                                          on_error=on_error):
            if item is not None:
                got.append(item)
        res = gate_and_backfill(batch, got, target_n, level=lv)
        kept.extend(res["kept"])
        rejected.extend(res["rejected"])
        for k, v in (res["reasons"] or {}).items():
            reasons[k] = reasons.get(k, 0) + v
        lv = res["level"]
        if not got:
            # 整批一条都没采到：多半是风控或选择器失效，再采只是白白消耗导航额度
            break

    # 达标不足且无法再采：对已采回但被淘汰的视频按下一档重判（数据已在手里，
    # 降档零成本零风险）；降档事实进 note，由调用方写日志与报告，不静默放宽标准
    relaxed: list[str] = []
    while len(kept) < min(MIN_KEEP_BEFORE_RELAX, target_n) and rejected:
        nxt = relax_level(lv)
        if not nxt:
            break
        relaxed.append(f"{lv}→{nxt}")
        lv = nxt
        still = []
        for it in rejected:
            if passes_gate(it, lv):
                it.quality_reject = ""
                kept.append(it)
            else:
                still.append(it)
        rejected = still
        reasons = {}
        for it in rejected:
            k = getattr(it, "quality_reject", "") or "未达标"
            reasons[k] = reasons.get(k, 0) + 1

    kept.sort(key=lambda x: (getattr(x, "quality_score", None) or 0.0), reverse=True)
    kept = kept[:target_n]
    likes = [it.like_count for it in kept if isinstance(it.like_count, int) and it.like_count > 0]
    note = (f"详情页导航 {fetched} 次 → 达标 {len(kept)}/{target_n} 条"
            + (f"；淘汰 " + "、".join(f"{k} {v}" for k, v in sorted(reasons.items())) if reasons else "")
            + (f"；素材不足已降档（{'，'.join(relaxed)}），未静默放宽标准" if relaxed else "")
            + (f"；门槛档 {lv}" if lv else "")
            + (f"；保留视频点赞 {min(likes)}~{max(likes)}" if likes else "")
            + ("；已触详情页导航硬上限（防会话级风控，不再多采）" if capped else "")
            + ("；候选池已用尽" if exhausted else ""))
    return {"items": kept, "rejected": rejected, "reasons": reasons, "fetched": fetched,
            "capped": capped, "exhausted": exhausted, "relaxed": relaxed,
            "level": lv, "note": note}
