"""频控器：每次网络动作之间随机等待，模拟正常浏览节奏。"""
import random
import time

from config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX


class RateLimiter:
    def __init__(self, min_s: float | None = None, max_s: float | None = None):
        self.min_s = min_s if min_s is not None else REQUEST_DELAY_MIN
        self.max_s = max_s if max_s is not None else REQUEST_DELAY_MAX
        if self.max_s < self.min_s:
            self.min_s, self.max_s = self.max_s, self.min_s

    def wait(self) -> float:
        seconds = random.uniform(self.min_s, self.max_s)
        time.sleep(seconds)
        return seconds
