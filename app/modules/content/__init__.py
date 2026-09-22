"""内容（Content）模块：boards/columns/qa 聚合。模块公共 API——跨模块 import 的唯一合法入口。"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.content.router import router

            _exported_routers = [router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            from app.modules.content.columns.graphql import ColumnsQuery
            from app.modules.content.graphql import ContentQuery

            _exported_graphql = [ContentQuery, ColumnsQuery]
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 惰性导出的公共名：显式声明让 `import *`、`dir()` 与 IDE/静态工具都能发现这两个入口
# （本包是跨模块 import 的唯一合法入口，名字被发现本身就是契约的一部分）
__all__: list[str] = ["GRAPHQL", "ROUTERS"]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
