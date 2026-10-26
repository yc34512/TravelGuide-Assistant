"""最小身份层：本地单用户或服务端 API Token。

默认 local_single_user 只算本地演示，不声称多用户隔离；api_token 模式下
令牌哈希映射 principal，owner 一律来自鉴权上下文，忽略客户端提交的归属字段。
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from config import AUTH_MODE, LOCAL_OWNER_ID


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    mode: str
    is_local: bool = False


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def local_principal() -> Principal:
    return Principal(id=LOCAL_OWNER_ID or "local_single_user",
                     mode="local_single_user", is_local=True)


def authenticate(authorization: str | None, store=None) -> Principal:
    mode = (AUTH_MODE or "local_single_user").lower()
    if mode == "local_single_user":
        return local_principal()
    if mode != "api_token":
        raise AuthError("未知 AUTH_MODE，仅支持 local_single_user / api_token")
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        raise AuthError("缺少 Bearer Token")
    token = raw.split(" ", 1)[1].strip()
    if not token:
        raise AuthError("缺少 Bearer Token")
    from core.storage import task_store
    store = store or task_store()
    row = store.principal_by_token_hash(hash_token(token))
    if row is None:
        raise AuthError("令牌无效或已禁用")
    return Principal(id=row["id"], mode="api_token", is_local=False)


def create_principal(owner_id: str, token: str | None = None, store=None) -> str:
    """服务端生成/登记令牌；返回明文令牌（只在创建时展示一次）。"""
    token = token or new_token()
    from core.storage import task_store
    store = store or task_store()
    store.put_principal(owner_id, hash_token(token), True)
    return token