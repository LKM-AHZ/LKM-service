"""评论（comment）域的 REST 路由。

步骤 9 切片 2 自 ``content.router`` 迁出：**路径逐字不变**（``/content/items/{id}/comments``
等），故对本模块而言 prefix 仍是 ``/content``，前端与 OpenAPI 调用方无需改动。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.comment import ContentCommentInfo
from app.modules.comment.models import ContentComment
from app.modules.comment.schemas import ContentCommentCreate
from app.modules.comment.service import (
    create_comment,
    delete_comment,
    like_comment,
    list_comments,
    unlike_comment,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from app.modules.rbac.service import check_owner
from core.common import (
    ApiResp,
    PageData,
    PaginateDep,
    PaginateParams,
)
from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.err import respond
from core.ports.authz import get_current_user, get_optional_user

router = APIRouter(prefix="/content", tags=["comment"])


@router.get(
    "/items/{item_id}/comments", response_model=ApiResp[PageData[ContentCommentInfo]]
)
@respond
async def list_content_comments(
    item_id: uuid.UUID,
    pag: PaginateParams = Depends(PaginateDep()),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[ContentCommentInfo]:
    """评论列表（楼层升序分页）。公开读；带登录态时回填每条评论的 ``liked``。"""
    return await list_comments(
        db,
        item_id,
        page=pag.page,
        limit=pag.limit,
        viewer_id=cur.id if cur is not None else None,
    )


@router.post("/items/{item_id}/comments", response_model=ApiResp[ContentCommentInfo])
@respond
async def create_content_comment(
    item_id: uuid.UUID,
    info: ContentCommentCreate,
    cur: CurrentUser = RequirePermission(Permission.content_comment_create),
    db: AsyncSession = Depends(get_session),
) -> ContentCommentInfo:
    return await create_comment(db, item_id, cur.id, info)


@router.post(
    "/items/{item_id}/comments/{comment_id}/like",
    response_model=ApiResp[dict[str, Any]],
)
@respond
async def like_content_comment(
    item_id: uuid.UUID,
    comment_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.content_like),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"like_count": await like_comment(db, item_id, comment_id, cur.id)}


@router.delete(
    "/items/{item_id}/comments/{comment_id}/like",
    response_model=ApiResp[dict[str, Any]],
)
@respond
async def unlike_content_comment(
    item_id: uuid.UUID,
    comment_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.content_like),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"like_count": await unlike_comment(db, item_id, comment_id, cur.id)}


@router.delete(
    "/items/{item_id}/comments/{comment_id}", response_model=ApiResp[dict[str, Any]]
)
@respond
async def delete_content_comment(
    item_id: uuid.UUID,
    comment_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # check_owner 顺带校验「评论存在且未软删」：不存在 → FORBIDDEN，不会静默删空
    await check_owner(
        db, cur, comment_id, ContentComment, "user_id", Permission.content_owner_delete
    )
    await delete_comment(db, item_id, comment_id)
    return {"ok": True}
