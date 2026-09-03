"""频控器：全局预约式令牌桶（合规第一道防线）。

设计要点：槽位状态是**类级共享**的——不管创建多少个实例、开多少个标签页、
起多少个线程，所有请求都排在同一条时间轴上，每次导航前预约一个槽位，
槽位之间的间隔就是随机频控延时。

所以并发提速不会提高风控风险：多 Tab 只是让"页面渲染与等待"并行，
单位时间内发往抖音域名的请求数与串行时完全一致。
"""
import random
import threading
import time

from config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX


class RateLimiter:
    """请求节奏控制器。线程安全；实例间共享全局队列。"""

    _lock = threading.Lock()   # 保护 _next_slot 的预约操作
    _next_slot = 0.0           # 全局时间轴上的下一个可放行时刻（epoch 秒）

    def __init__(self, min_s: float | None = None, max_s: float | None = None):
        self.min_s = min_s if min_s is not None else REQUEST_DELAY_MIN
        self.max_s = max_s if max_s is not None else REQUEST_DELAY_MAX
        if self.max_s < self.min_s:
            self.min_s, self.max_s = self.max_s, self.min_s

    def wait(self) -> float:
        """预约下一个全局槽位并睡到该时刻，返回实际等待秒数。

        并发场景下后到的线程会自动排到队尾（等待时间随队列深度增长），
        这正是"请求间隔不缩短"的实现方式。"""
        with RateLimiter._lock:
            now = time.time()
            start = max(now, RateLimiter._next_slot)
            RateLimiter._next_slot = start + random.uniform(self.min_s, self.max_s)
            delay = start - now
        if delay > 0:
            time.sleep(delay)
        return delay

    @classmethod
    def reset(cls) -> None:
        """清空全局队列（测试与长任务重启时用，避免历史预约拖累首次请求）。"""
        with cls._lock:
            cls._next_slot = 0.0


_GLOBAL = RateLimiter()


def global_limiter() -> RateLimiter:
    """共享频控器：多 Tab / 多线程采集必须用同一个，保证请求间隔统一兜底。"""
    return _GLOBAL
