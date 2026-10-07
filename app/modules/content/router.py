import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# 内容域子路由：版块 / 专栏 / 问答 统一挂到 /content 前缀下（逐域子前缀）。
from app.modules.content.boards.router import router as _boards_router
from app.modules.content.columns.router import router as _columns_router
from app.modules.content.models import Column, ContentComment, ContentItem, ContentType
from app.modules.content.qa.router import router as _qa_router
from app.modules.content.schemas import (
    ContentCommentCreate,
    ContentCommentInfo,
    ContentItemCreate,
    ContentItemInfo,
)
from app.modules.content.service import (
    create_comment,
    create_item,
    delete_comment,
    delete_item,
    like_comment,
    like_item,
    list_comments,
    unlike_comment,
    unlike_item,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from app.modules.rbac.service import check_owner, user_has_permission
from core.common import (
    ApiResp,
    PageData,
    PaginateDep,
    PaginateParams,
)
from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.err import BizError, CommonErr, respond
from core.ports.authz import get_current_user, get_optional_user

router = APIRouter(prefix="/content", tags=["content"])
router.include_router(_boards_router)
router.include_router(_columns_router)
router.include_router(_qa_router)


@router.post("/items", response_model=ApiResp[ContentItemInfo])
@respond
async def create_content_item(
    info: ContentItemCreate,
    cur: CurrentUser = RequirePermission(Permission.content_create),
    db: AsyncSession = Depends(get_session),
) -> ContentItemInfo:
    if info.content_type in (ContentType.BLOG_POST, ContentType.QA):
        # 博客发布和提问各有自己的写入口，直接写统一内容表会绕过其审核与关联流程。
        raise BizError(CommonErr.FORBIDDEN)
    if info.content_type == ContentType.ARTICLE and not await user_has_permission(
        db, cur, Permission.articles_publish
    ):
        raise BizError(CommonErr.FORBIDDEN)
    if info.content_type == ContentType.COLUMN_POST:
        if not await user_has_permission(db, cur, Permission.columns_publish):
            raise BizError(CommonErr.FORBIDDEN)
        if info.column_id:
            await check_owner(
                db,
                cur,
                info.column_id,
                Column,
                "owner_id",
                Permission.column_owner_publish,
            )
    if (info.is_pinned or info.is_featured) and not await user_has_permission(
        db, cur, Permission.admin_content_review
    ):
        raise BizError(CommonErr.FORBIDDEN)
    return await create_item(db, cur.id, info)


@router.post("/items/{item_id}/like", response_model=ApiResp[dict[str, Any]])
@respond
async def like_content_item(
    item_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.content_like),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"like_count": await like_item(db, item_id, cur.id)}


@router.delete("/items/{item_id}/like", response_model=ApiResp[dict[str, Any]])
@respond
async def unlike_content_item(
    item_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.content_like),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"like_count": await unlike_item(db, item_id, cur.id)}


@router.delete("/items/{item_id}", response_model=ApiResp[dict[str, Any]])
@respond
async def delete_content_item(
    item_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await check_owner(
        db, cur, item_id, ContentItem, "author_id", Permission.content_owner_delete
    )
    await delete_item(db, item_id, cur.id)
    return {"ok": True}


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
    "/items/{item_id}/comments/{comment_id}/like", response_model=ApiResp[dict[str, Any]]
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
    "/items/{item_id}/comments/{comment_id}/like", response_model=ApiResp[dict[str, Any]]
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
