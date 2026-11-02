"""blog 域端口：后台删除博客系列/评论的写入口。

步骤 9 切片 3 把博客拆成独立域后，content 的后台 API（``delete_series`` /
``delete_blog_comment``）不能再直接 import 博客服务，改经这里转发；权限与级联语义仍由
博客域自己实现（``as_admin`` 决定是否跳过属主校验）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def delete_series(
    db: AsyncSession,
    series_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """删除博客系列（返回其条目 id），供后台内容管理使用。"""
    return await get("blog_api").delete_series(
        db, series_id, actor_id, as_admin=as_admin
    )


async def delete_comment(
    db: AsyncSession,
    series_id: uuid.UUID,
    comment_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """删除博客评论（返回系列 id），供后台内容管理使用。"""
    return await get("blog_api").delete_comment(
        db, series_id, comment_id, actor_id, as_admin=as_admin
    )
