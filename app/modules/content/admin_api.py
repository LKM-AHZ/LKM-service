"""content 域的后台管理实现（``core.ports.content_admin`` 的去处）。

纯转发：把端口调用映射到本域三个服务模块。放在 content 包内，是因为 admin 不该知道
内容在内部被拆成了帖子（service）/ 文章（articles）/ 专栏（blog）三块。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def list_items(
    db: AsyncSession, *, page: int, limit: int, content_type: str | None
) -> Any:
    from app.modules.content.service import list_items as _list_items

    return await _list_items(db, page=page, limit=limit, content_type=content_type)


async def delete_item(
    db: AsyncSession, item_id: uuid.UUID, actor_id: uuid.UUID, *, as_admin: bool = True
) -> uuid.UUID:
    from app.modules.content.service import delete_item as _delete_item

    return await _delete_item(db, item_id, actor_id, as_admin=as_admin)


async def delete_series(
    db: AsyncSession,
    series_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """后台删博客系列：博客已是独立域，经 ``core.ports.blog`` 调它的写入口。"""
    from core.ports import blog as blog_port

    return await blog_port.delete_series(db, series_id, actor_id, as_admin=as_admin)


async def delete_blog_comment(
    db: AsyncSession,
    series_id: uuid.UUID,
    comment_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """后台删博客评论：同上，经博客域端口。"""
    from core.ports import blog as blog_port

    return await blog_port.delete_comment(
        db, series_id, comment_id, actor_id, as_admin=as_admin
    )


async def delete_article_comment(
    db: AsyncSession,
    comment_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    from app.modules.content.articles.service import (
        delete_article_comment as _delete_article_comment,
    )

    return await _delete_article_comment(db, comment_id, actor_id, as_admin=as_admin)
