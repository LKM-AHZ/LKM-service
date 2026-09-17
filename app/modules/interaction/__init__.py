"""interaction 模块（M6.6）：收藏 + 浏览记录。

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
            from app.modules.interaction.router import router

            _exported_routers = [router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            _exported_graphql = []
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
