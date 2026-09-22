"""后台内容删除端点：/admin/content/*（管理员删除用户内容）。

高风险写操作：须持有有效后台 2FA 信任（require_admin_2fa，1 小时窗口），
删除后记录审计（谁删了谁的内容）。仅 account_level=admin 可访问。
普通用户删除自己的内容走各自的前台端点（无 2FA）。
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ip import client_ip
from app.core.common import ApiResp, PageData, PaginateDep, PaginateParams
from app.core.err import respond
from app.db.session import get_session
from app.modules.articles.service import (
    delete_article_comment as delete_article_comment_svc,
)
from app.modules.blog.service import (
    delete_comment as delete_blog_comment_svc,
)
from app.modules.blog.service import (
    delete_series as delete_blog_series_svc,
)
from app.modules.content.schemas import ContentItemInfo
from app.modules.content.service import (
    delete_item as delete_content_item_svc,
)
from app.modules.content.service import list_items as list_content_items_svc
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser
from auth.seams import log_audit

from .deps import require_admin, require_admin_2fa
from .permissions import require_permission

router = APIRouter(prefix="/admin/content", tags=["admin-content"])


@router.get("/items", response_model=ApiResp[PageData[ContentItemInfo]])
@respond
async def admin_list_content_items(
    _cur: CurrentUser = require_admin,
    pag: PaginateParams = Depends(PaginateDep()),
    content_type: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> PageData[ContentItemInfo]:
    """管理员内容列表（只读）：统一内容项分页，可按 content_type 筛选。

    原管理端帖子列表走已删除的 `/forum/posts`，这里以 content_items 单表承载
    （discussion/article/column_post/blog_post/qa），供后台帖子/内容管理页使用。
    """
    await require_permission(db, _cur, Permission.admin_content_review)
    return await list_content_items_svc(
        db, page=pag.page, limit=pag.limit, content_type=content_type
    )


async def _audit_admin_delete(
    db: AsyncSession,
    request: Request,
    admin_id: uuid.UUID,
    target_user_id: uuid.UUID,
    action: str,
    detail: str,
) -> None:
    """记录管理员删除内容审计：user_id=被删内容作者，detail 含内容与执行者。"""
    await log_audit(
        db,
        target_user_id,
        action,
        detail=f"{detail} by admin={admin_id}",
        ip_address=client_ip(request),
    )


@router.delete("/item/{item_id}", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_delete_content_item(
    item_id: uuid.UUID,
    request: Request,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """管理员删除统一内容项（讨论帖/官方文章/专栏连载/博客发布产物）。"""
    await require_permission(db, cur, Permission.admin_content_review)
    author_id = await delete_content_item_svc(db, item_id, cur.id, as_admin=True)
    await _audit_admin_delete(
        db,
        request,
        cur.id,
        author_id,
        "admin_delete_content_item",
        f"item={item_id}",
    )
    return {"ok": True}


@router.delete("/series/{series_id}", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_delete_series(
    series_id: uuid.UUID,
    request: Request,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """管理员删除用户专栏系列。"""
    await require_permission(db, cur, Permission.admin_content_review)
    owner_id = await delete_blog_series_svc(db, series_id, cur.id, as_admin=True)
    await _audit_admin_delete(
        db,
        request,
        cur.id,
        owner_id,
        "admin_delete_series",
        f"series={series_id}",
    )
    return {"ok": True}


@router.delete(
    "/blog-comment/{series_id}/{comment_id}",
    response_model=ApiResp[dict[str, Any]],
)
@respond
async def admin_delete_blog_comment(
    series_id: uuid.UUID,
    comment_id: uuid.UUID,
    request: Request,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """管理员删除用户博客系列评论。"""
    await require_permission(db, cur, Permission.admin_content_review)
    author_id = await delete_blog_comment_svc(
        db, series_id, comment_id, cur.id, as_admin=True
    )
    await _audit_admin_delete(
        db,
        request,
        cur.id,
        author_id,
        "admin_delete_blog_comment",
        f"series={series_id} comment={comment_id}",
    )
    return {"ok": True}


@router.delete("/article-comment/{comment_id}", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_delete_article_comment(
    comment_id: uuid.UUID,
    request: Request,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """管理员删除用户文章评论。"""
    await require_permission(db, cur, Permission.admin_content_review)
    author_id = await delete_article_comment_svc(db, comment_id, cur.id, as_admin=True)
    await _audit_admin_delete(
        db,
        request,
        cur.id,
        author_id,
        "admin_delete_article_comment",
        f"comment={comment_id}",
    )
    return {"ok": True}
