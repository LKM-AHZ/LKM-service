"""认证与用户档案包：登录/OAuth/2FA/passkey/恢复/引导/设置。

对 app 侧的公共 API——跨包 import 的唯一合法入口（另有 ``auth.deps``/``auth.schemas``/
``auth.snapshot``/``auth.seams``/``auth.entities`` 五个公开面模块）。所有 router 均惰性
import，保证 `import auth` 绝对轻量且零循环。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None
_exported_snapshot: Any | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql, _exported_snapshot
    # 统一只读身份缝（A1）：惰性载入，保持 `import auth` 绝对轻量零循环。
    # 业务域展示性身份读取只经 auth.snapshot（见 M3 spec 读缝契约）。
    if name in ("UserSnapshot", "get_user_snapshot", "get_user_snapshot_batch"):
        if _exported_snapshot is None:
            from auth import snapshot as _exported_snapshot
        return getattr(_exported_snapshot, name)
    if name == "ROUTERS":
        if _exported_routers is None:
            from auth.router import router
            from auth.router_2fa import router as router_2fa
            from auth.router_authz import router as router_authz
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
    # 注册钩子（app 侧基础设施枢纽调用）：惰性转发，保持 `import auth` 零副作用。
    if name in ("register_models", "register_tasks", "register_errors"):
        from auth import register as _register

        return getattr(_register, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
