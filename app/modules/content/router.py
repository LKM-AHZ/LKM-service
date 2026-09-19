import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import (
    ApiResp,
)
from app.core.err import respond
from app.db.session import get_session

# 内容域子路由：版块 / 专栏 / 问答 统一挂到 /content 前缀下（逐域子前缀）。
from app.modules.content.boards.router import router as _boards_router
from app.modules.content.columns.router import router as _columns_router
from app.modules.content.models import ContentItem
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
    delete_item,
    like_item,
    unlike_item,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from app.modules.rbac.service import check_owner
from auth.deps import CurrentUser, get_current_user

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
    return await create_item(db, cur.id, info)


@router.post("/items/{item_id}/like", response_model=ApiResp[dict[str, Any]])
@respond
async def like_content_item(
    item_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"like_count": await like_item(db, item_id, cur.id)}


@router.delete("/items/{item_id}/like", response_model=ApiResp[dict[str, Any]])
@respond
async def unlike_content_item(
    item_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
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


@router.post("/items/{item_id}/comments", response_model=ApiResp[ContentCommentInfo])
@respond
async def create_content_comment(
    item_id: uuid.UUID,
    info: ContentCommentCreate,
    cur: CurrentUser = RequirePermission(Permission.content_comment_create),
    db: AsyncSession = Depends(get_session),
) -> ContentCommentInfo:
    return await create_comment(db, item_id, cur.id, info)
