"""可丢弃缓存：Redis 适配 + NoOp 降级。

Redis 只保存可重建的缓存、限流窗口和同键回源合并，不保存任务、报告或预算事实。
模块导入不联网；连接在第一次使用时建立。任何 Redis 故障都退化为“缓存未命中”，
由调用方有界回源，绝不把故障表现成“没有这份数据”。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from urllib.parse import quote

from config import CACHE_ENABLED, EMBEDDING_MODEL, REDIS_URL

SCHEMA = "v1"
CHUNKER_VERSION = "chunks-v1"
_MISS = {"__cache_miss__": True}


def _q(value) -> str:
    return quote(str(value or ""), safe="")


def route_key(origin: str, destination: str, mode: str, departure_bucket: str,
              coordinate_system: str = "gcj02") -> str:
    """交通缓存必须含坐标系、方向、方式与出发时间（分桶）。"""
    return (f"tg:{SCHEMA}:route:{_q(coordinate_system)}:{_q(origin)}:"
            f"{_q(destination)}:{_q(mode)}:{_q(departure_bucket)}")


def poi_key(entity_id: str, material_version: str, profile_policy: str) -> str:
    return f"tg:{SCHEMA}:poi:{_q(entity_id)}:{_q(material_version)}:{_q(profile_policy)}"


def heat_key(city_id: str, source_version: str) -> str:
    return f"tg:{SCHEMA}:heat:{_q(city_id)}:{_q(source_version)}"


def context_key(owner_id: str, report_version: str, query: str,
                embedding_model: str = "", chunker_version: str = CHUNKER_VERSION) -> str:
    """私人报告上下文：owner、报告版本、模型、切块版本、问题全部参与 key。"""
    qh = hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:24]
    return (f"tg:{SCHEMA}:context:{_q(owner_id)}:{_q(report_version)}:"
            f"{_q(embedding_model or EMBEDDING_MODEL)}:{_q(chunker_version)}:{qh}")


def departure_bucket(now: float | None = None, seconds: int = 300) -> str:
    n = int((time.time() if now is None else now) // max(1, seconds))
    return f"b{n}"


class NoOpCache:
    enabled = False
    backend = "noop"

    def get(self, key: str):
        return None

    def set(self, key: str, value, ttl: int, meta: dict | None = None) -> bool:
        return False

    def delete(self, key: str) -> None:
        return None

    def incr_window(self, key: str, limit: int, window_seconds: int) -> dict:
        return {"allowed": True, "count": None, "backend": "noop"}

    def ping(self) -> dict:
        return {"ok": True, "backend": "noop", "enabled": False}

    def get_or_compute(self, key: str, ttl: int, loader, *, negative_ttl: int = 30,
                       cache_negative: bool | None = None):
        return loader(), False


class RedisCache:
    enabled = True
    backend = "redis"

    def __init__(self, url: str | None = None):
        self.url = (url if url is not None else REDIS_URL).strip()
        self._client = None
        self._lock = threading.Lock()
        self.errors = 0

    def _get_client(self):
        if not self.url:
            return None
        with self._lock:
            if self._client is None:
                try:
                    import redis
                    self._client = redis.Redis.from_url(
                        self.url, decode_responses=True, socket_connect_timeout=1,
                        socket_timeout=1, retry_on_timeout=False)
                except Exception:
                    self.errors += 1
                    return None
        return self._client

    def get(self, key: str):
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(key)
            if raw is None:
                return None
            payload = json.loads(raw)
            if payload.get("v") != SCHEMA:
                return None
            value = payload.get("value")
            if value == _MISS:
                return _MISS
            return value
        except Exception:
            self.errors += 1
            return None

    def set(self, key: str, value, ttl: int, meta: dict | None = None) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            payload = {"v": SCHEMA, "value": value, "created_at": time.time(),
                       "meta": meta or {}}
            client.setex(key, max(1, int(ttl)), json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            self.errors += 1
            return False

    def delete(self, key: str) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(key)
        except Exception:
            self.errors += 1

    def incr_window(self, key: str, limit: int, window_seconds: int) -> dict:
        """固定窗口限流；Redis 不可用时返回 allowed=True，由持久额度/队列兜底。"""
        client = self._get_client()
        if client is None:
            return {"allowed": True, "count": None, "backend": "redis_unavailable"}
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, max(1, int(window_seconds)), nx=True)
            count = int(pipe.execute()[0])
            return {"allowed": count <= int(limit), "count": count,
                    "remaining": max(0, int(limit) - count), "backend": "redis"}
        except Exception:
            self.errors += 1
            return {"allowed": True, "count": None, "backend": "redis_unavailable"}

    def ping(self) -> dict:
        client = self._get_client()
        if client is None:
            return {"ok": False, "backend": "redis", "enabled": True,
                    "error": "未配置或客户端不可用", "errors": self.errors}
        try:
            return {"ok": bool(client.ping()), "backend": "redis", "enabled": True,
                    "errors": self.errors}
        except Exception as exc:
            return {"ok": False, "backend": "redis", "enabled": True,
                    "error": type(exc).__name__, "errors": self.errors}

    def get_or_compute(self, key: str, ttl: int, loader, *, negative_ttl: int = 30,
                       cache_negative: bool | None = None):
        cached = self.get(key)
        if cached is not None:
            if cached == _MISS:
                return None, True
            return cached, True
        # 同键回源合并：进程内 single-flight；跨进程仍靠唯一键/条件写入兜底。
        lock = _locks.setdefault(key, threading.Lock())
        with lock:
            cached = self.get(key)
            if cached is not None:
                if cached == _MISS:
                    return None, True
                return cached, True
            value = loader()
            if value is None:
                if cache_negative is not False:
                    self.set(key, _MISS, negative_ttl, {"negative": True})
                return None, False
            self.set(key, value, ttl)
            return value, False


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def get_cache():
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = RedisCache() if CACHE_ENABLED and REDIS_URL else NoOpCache()
        return _INSTANCE


def set_cache(cache) -> None:
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = cache


def cache_status() -> dict:
    try:
        return get_cache().ping()
    except Exception as exc:
        return {"ok": False, "backend": "unknown", "error": type(exc).__name__}