"""服务端主体与 API Token 管理（只用于 AUTH_MODE=api_token）。

创建（明文令牌只显示一次）：
    python scripts/manage_principal.py create --owner alice
禁用：
    python scripts/manage_principal.py disable --owner alice
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from core.auth import create_principal, hash_token
from core.storage import task_store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TravelGuide 主体令牌管理")
    parser.add_argument("action", choices=("create", "disable"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--token", default="", help="自备令牌；缺省由服务端生成")
    args = parser.parse_args(argv)
    store = task_store()
    if args.action == "create":
        token = create_principal(args.owner, args.token or None, store=store)
        print(f"owner={args.owner}")
        print(f"token={token}")
        print("明文令牌只显示这一次；请放入客户端环境变量 TG_API_TOKEN。")
        return 0
    # disable：保留唯一约束，清空 token_hash；不可再由该令牌认证。
    store.put_principal(args.owner, None, enabled=False)
    print(f"owner={args.owner} 已禁用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())