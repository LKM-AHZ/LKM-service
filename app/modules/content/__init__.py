"""内容（Content）模块：boards/columns/qa/**articles/blog** 聚合。模块公共 API——跨模块 import 的唯一合法入口。

蓝图 M2「内容域收敛」：articles 与 blog 原先各自是独立业务域（独立表 + 交叉 import），
现并入本聚合（**目录级合并，路由前缀与表结构一字不变**——M2 验收明许「过渡期可目录合并
+ 路由拆片」）。四类子包与 boards/columns/qa 完全同构，各自是路由拆片单元；本文件的
``ROUTERS`` / ``GRAPHQL`` 是它们对外的唯一出口。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.content.articles.router import router as articles_router
            from app.modules.content.blog.git_http import git_router
            from app.modules.content.blog.router import router as blog_router
            from app.modules.content.router import router

            # 顺序 = 聚合顺序；各 router 自带 prefix（/content、/articles、/blog、/blog/git），
            # 故合并前后 URL 完全不变
            _exported_routers = [router, articles_router, blog_router, git_router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            from app.modules.content.articles.graphql import ArticlesQuery
            from app.modules.content.blog.graphql import BlogQuery
            from app.modules.content.columns.graphql import ColumnsQuery
            from app.modules.content.graphql import ContentQuery

            _exported_graphql = [
                ContentQuery,
                ColumnsQuery,
                ArticlesQuery,
                BlogQuery,
            ]
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 惰性导出的公共名：显式声明让 `import *`、`dir()` 与 IDE/静态工具都能发现这两个入口
# （本包是跨模块 import 的唯一合法入口，名字被发现本身就是契约的一部分）
__all__: list[str] = ["GRAPHQL", "ROUTERS"]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
