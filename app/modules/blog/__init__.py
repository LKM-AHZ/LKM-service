"""博客（blog）模块：系列/文件/评论/仓库隔离与 git 智能 HTTP 面。

原为独立业务域，M2「内容域收敛」时并入 content 聚合；步骤 9 切片 3 又拆出来——判据与
问答/评论一致：自己的表（``blog_*``）、自己的错误码命名空间、自己的调度任务。路由前缀
（``/blog``、``/blog/git``）与表名一字未改。

与 content 的耦合两处，均经端口：

- 发布要落一条 ``content_items``（content_type=blog_post）→ ``core.ports.content.publish_blog_item``；
- 发布时按 slug 解析板块 → ``core.ports.content.ensure_board``；
- 反向：后台删系列/删评论由 content 的 admin_api 经 ``core.ports.blog`` 调进来。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.blog.git_http import git_router
            from app.modules.blog.router import router

            _exported_routers = [router, git_router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            from app.modules.blog.graphql import BlogQuery

            _exported_graphql = [BlogQuery]
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: list[str] = ["GRAPHQL", "ROUTERS"]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
