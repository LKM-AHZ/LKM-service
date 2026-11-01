"""内容域的后台管理端口。

后台内容管理（删除帖子/专栏/评论、列内容）本质上是在调用 content 域的业务操作，
原先 `admin.content_router` 直接 import 三个 content 服务模块。改走端口后，admin 只依赖
这组签名，content 内部怎么拆分（帖子/专栏/问答）与 admin 无关。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def list_items(
    db: AsyncSession, *, page: int, limit: int, content_type: str | None
) -> Any:
    """后台内容列表（分页）。"""
    return await get("content_admin").list_items(
        db, page=page, limit=limit, content_type=content_type
    )


async def delete_item(
    db: AsyncSession, item_id: uuid.UUID, actor_id: uuid.UUID, *, as_admin: bool = True
) -> uuid.UUID:
    """删除内容项，返回作者 id。"""
    return await get("content_admin").delete_item(
        db, item_id, actor_id, as_admin=as_admin
    )


async def delete_series(
    db: AsyncSession,
    series_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """删除专栏系列，返回作者 id。"""
    return await get("content_admin").delete_series(
        db, series_id, actor_id, as_admin=as_admin
    )


async def delete_blog_comment(
    db: AsyncSession,
    series_id: uuid.UUID,
    comment_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """删除专栏评论，返回作者 id。"""
    return await get("content_admin").delete_blog_comment(
        db, series_id, comment_id, actor_id, as_admin=as_admin
    )


async def delete_article_comment(
    db: AsyncSession,
    comment_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    as_admin: bool = True,
) -> uuid.UUID:
    """删除文章评论，返回作者 id。"""
    return await get("content_admin").delete_article_comment(
        db, comment_id, actor_id, as_admin=as_admin
    )
