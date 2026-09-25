"""interaction 模块（M6.6）：收藏 + 浏览记录 + **关注关系**。

蓝图 §7.2 的目标形态把本模块定义为「收窄：收藏、关注、浏览记录」——关注关系原属 feed
域，现迁入本模块（信息流域只保留时间线生成）。REST URL 与 GraphQL 字段名一律不破。

跨模块唯一合法入口；ROUTERS/GRAPHQL 惰性加载（照 points 范式），避免 import 时
拉起重型依赖。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.interaction.router import (
                board_follow_router,
                router,
                user_follow_router,
            )

            _exported_routers = [router, user_follow_router, board_follow_router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            from app.modules.interaction.graphql import FollowQuery

            _exported_graphql = [FollowQuery]
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
