"""评论（comment）模块：统一内容评论与评论点赞（步骤 9 切片 2，自 ``content`` 拆出）。

``content_comments`` / ``content_comment_likes`` 两张表与「评论不存在」错误码（沿用
content 命名空间 17.2）随本模块走；REST 路径仍是 ``/content/items/{id}/comments``。

与 content 的耦合双向，各配一条端口：

- 本域 → content：发评论/点赞前锁帖子行并断言存在（``require_content_item`` /
  ``lock_content_item``）、评论数写穿（``bump_comment_count``）；
- content → 本域：``reconcile_counts`` 的 ``comment_count`` 标量子查询、通知的评论摘要、
  举报队列的评论正文，都经 ``core.ports.comment``。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None

#: 评论域没有独立 GraphQL 字段（评论树由 content 的 ``ContentQuery`` 组装，读口经端口）。
GRAPHQL: list[Any] = []


def __getattr__(name: str) -> Any:
    global _exported_routers
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.comment.router import router

            _exported_routers = [router]
        return _exported_routers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: list[str] = ["GRAPHQL", "ROUTERS"]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
