"""后台管理子系统（仅 account_level=admin 可访问）。

认证采用独立的 httpOnly Cookie 会话（admin_session / admin_refresh），
与前台 localStorage+Bearer 双轨并存。权限单一事实源在后端 get_current_admin。

模块公共 API——跨模块 import 的唯一合法入口。所有 router 均惰性 import。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.admin.analytics_router import router as router_analytics
            from app.modules.admin.auth_router import router
            from app.modules.admin.bot_router import router as router_bot
            from app.modules.admin.content_router import router as router_content
            from app.modules.admin.dlq_router import router as router_dlq
            from app.modules.admin.moderation.admin_router import (
                router as router_moderation,
            )
            from app.modules.admin.reports_router import router as router_reports
            from app.modules.admin.users_router import router as router_users

            _exported_routers = [
                router,
                router_content,
                router_users,
                router_reports,
                router_dlq,
                router_moderation,
                router_analytics,
                router_bot,
            ]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            _exported_graphql = []
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
