"""信息流(feed)域：read-time 时间线(read 合流 + 关注过滤 + 审校降权)。

**关注关系不在本域**：蓝图 §3.1 与 §7.2 的目标形态都把「关注」划给 interaction
（「收窄：收藏、关注、浏览记录」），而信息流域只负责「时间线生成」。故 follow 的模型/
仓储/服务/路由/GraphQL 已迁入 ``app.modules.interaction``（REST URL 与 GraphQL 字段名
一字不变）；本域只经 interaction 的公开读口消费关注关系。

模块公共 API——跨模块 import 的唯一合法入口。
``ROUTERS`` = timeline_router(/timeline…)；``GRAPHQL`` = TimelineQuery。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.feed.router import timeline_router

            _exported_routers = [timeline_router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            from app.modules.feed.graphql import TimelineQuery

            _exported_graphql = [TimelineQuery]
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
