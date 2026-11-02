import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# 内容域子路由：版块 / 专栏挂到 /content 前缀下（问答已是独立模块 app.modules.qa）。
from app.modules.content.boards.router import router as _boards_router
from app.modules.content.columns.router import router as _columns_router
from app.modules.content.models import Column, ContentItem, ContentType
from app.modules.content.schemas import (
    ContentItemCreate,
    ContentItemInfo,
    ContentReportCreate,
)
from app.modules.content.service import (
    create_item,
    delete_item,
    forward_item,
    like_item,
    report_content,
    unlike_item,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from app.modules.rbac.service import check_owner, user_has_permission
from core.common import (
    ApiResp,
)
from core.contracts import CurrentUser
from core.db.session import get_session
from core.err import BizError, CommonErr, respond
from core.ports.authz import get_current_user

router = APIRouter(prefix="/content", tags=["content"])
router.include_router(_boards_router)
router.include_router(_columns_router)


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


@router.post("/items/{item_id}/forward", response_model=ApiResp[dict[str, Any]])
@respond
async def forward_content_item(
    item_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.content_forward),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """转发上报：由前端在「链接已复制」之后调用，返回新的转发数。"""
    return {"forward_count": await forward_item(db, item_id)}


@router.post("/reports", response_model=ApiResp[dict[str, Any]])
@respond
async def report_content_target(
    info: ContentReportCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """提交内容举报（帖子 / 评论），进后台「举报」队列。

    刻意只用 ``get_current_user``、不挂权限点：举报是安全出口，把 local（未绑定邮箱/手机号）
    账户挡在外面等于让他们无法举报违规内容——这与点赞/收藏的档位门槛是两种性质。
    """
    await report_content(db, cur.id, info.target_type, info.target_id, info.reason)
    return {"ok": True}


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
