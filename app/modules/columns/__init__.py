"""专栏（columns）模块：专栏申请/审核、连载与只读 GraphQL。

步骤 9 切片 3 自 content 拆出：``columns`` / ``column_applications`` / ``column_posts``
三张表、``NS_COLUMNS`` 错误码、REST 面（前缀仍是 ``/content/columns``）与 ColumnsQuery
随域迁移；表名与 URL 一字未改。

与 content 的耦合：

- 本域 → content：无（连载写自己的表，内容项由 content 侧建）；
- content → 本域：建 column_post 时校验专栏存在（``column_exists``）、列表/详情回填
  ``column_title``（``column_titles``）、feed 的专栏源（``select_feed_source``）、
  后台待审队列（``pending_column_applications``），全经 ``core.ports.columns``。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.columns.router import router

            _exported_routers = [router]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            from app.modules.columns.graphql import ColumnsQuery

            _exported_graphql = [ColumnsQuery]
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: list[str] = ["GRAPHQL", "ROUTERS"]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
