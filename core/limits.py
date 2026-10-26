"""持久容量闸门：全局在途模型调用槽位与 Redis 限流。

槽位由数据库权威状态维护（SQLite/MySQL resource_leases），Redis 只做加速限流。
租约到期不自动证明旧执行者已停止；这里只限制本地已发起的在途调用，
远端服务商是否执行仍需由请求账本记录为 known/unknown。
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from config import GLOBAL_MODEL_INFLIGHT
from core.cache import get_cache

MODEL_SLOT_TTL = 180.0


class SlotUnavailable(RuntimeError):
    pass


class SlotLease:
    def __init__(self, store, resource: str, owner: str, job_id: str,
                 capacity: int, ttl: float = MODEL_SLOT_TTL):
        self.store = store
        self.resource = resource
        self.owner = owner
        self.job_id = job_id
        self.capacity = capacity
        self.ttl = ttl
        self.lease_id: str | None = None

    def __enter__(self):
        self.lease_id = self.store.acquire_slot(
            self.resource, self.owner, self.job_id, self.capacity, self.ttl)
        if self.lease_id is None:
            raise SlotUnavailable(f"{self.resource} 容量已满")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.lease_id:
            self.store.release_slot(self.lease_id)
            self.lease_id = None
        return False


def model_slot(store, owner: str, job_id: str, capacity: int | None = None) -> SlotLease:
    return SlotLease(store, "model:global", owner, job_id,
                     capacity or GLOBAL_MODEL_INFLIGHT)


def rate_limit(scope: str, identity: str, limit: int, window_seconds: int) -> dict:
    """Redis 固定窗口限流；不可用时 allowed=True（持久队列/额度仍兜底）。"""
    key = f"tg:v1:rl:{scope}:{identity}"
    return get_cache().incr_window(key, limit, window_seconds)


def try_acquire_model_slot(store, owner: str, job_id: str,
                           timeout_seconds: float = 0.0) -> SlotLease | None:
    """有界等待；超时返回 None，由调用方降级或拒绝，不无限排队。"""
    deadline = time.time() + max(0.0, timeout_seconds)
    while True:
        lease = SlotLease(store, "model:global", owner, job_id, GLOBAL_MODEL_INFLIGHT)
        try:
            lease.__enter__()
            return lease
        except SlotUnavailable:
            if time.time() >= deadline:
                return None
            time.sleep(0.05)