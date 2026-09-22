"""search 模块（M6.9）：站内只读检索（P1，PG FTS + pg_trgm）。

跨模块唯一合法入口；ROUTERS 惰性加载（照 points/interaction 范式），避免 import
时拉起重型依赖。本模块**无自有表**（纯读聚合），故不参与 db.model_registry。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.search.router import router

            _exported_routers = [router]
        return _exported_routers
    if name == "GRAPHQL":
        # 与 points/interaction 等模块同款：缓存单例，保证身份稳定（mod.GRAPHQL is mod.GRAPHQL）
        if _exported_graphql is None:
            _exported_graphql = []
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
