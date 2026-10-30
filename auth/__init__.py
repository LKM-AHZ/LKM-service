"""认证与用户档案包：登录/OAuth/2FA/passkey/恢复/引导/设置。

本包向 ``auth.bootstrap`` 提供路由与注册钩子，并保留惰性导出。
业务包 app 通过 ``core.ports`` 与共享契约协作，不直接 import auth；
两侧注册项由 ``boot.assemble`` 汇总。router 惰性导入，避免包初始化时加载完整路由树。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None
_exported_snapshot: Any | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql, _exported_snapshot
    if name in ("UserSnapshot", "get_user_snapshot", "get_user_snapshot_batch"):
        if _exported_snapshot is None:
            # 显式 import_module：`from auth import snapshot as ...` 依赖「__getattr__ 抛
            # AttributeError → import 机制回退子模块」这一隐式行为，会把 snapshot.py 内部
            # 真正的 AttributeError 与「本模块无此属性」混为一谈（hasattr 也会假报 False）
            import importlib

            _exported_snapshot = importlib.import_module("auth.snapshot")
        return getattr(_exported_snapshot, name)
    if name == "ROUTERS":
        if _exported_routers is None:
            from auth.router import router
            from auth.router_2fa import router as router_2fa
            from auth.router_authz import router as router_authz
            from auth.router_bot_sso import router as router_bot_sso
            from auth.router_oauth import router as router_oauth
            from auth.router_onboarding import router as router_onboarding
            from auth.router_passkey import router as router_passkey
            from auth.router_read import (
                router as router_read,  # B1.2 内部读面
            )
            from auth.router_recovery import router as router_recovery
            from auth.router_settings import router as router_settings

            _exported_routers = [
                router,
                router_2fa,
                router_authz,
                router_bot_sso,
                router_oauth,
                router_onboarding,
                router_passkey,
                router_read,
                router_recovery,
                router_settings,
            ]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            _exported_graphql = []
        return _exported_graphql
    # 注册钩子由 auth.bootstrap 调用；惰性转发避免包导入时加载实现。
    if name in ("register_models", "register_tasks", "register_errors"):
        from auth import register as _register

        return getattr(_register, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
