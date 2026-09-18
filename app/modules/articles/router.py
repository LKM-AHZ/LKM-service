import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import respond
from app.db.session import get_session
from app.modules.articles.models import ArticleComment
from app.modules.articles.schemas import (
    ArticleCommentCreate,
    ArticleCommentOut,
    ArticleCreate,
    ArticleDetail,
    ArticleLikeStatus,
    ArticleUpdate,
    CategoryCreate,
    CategoryOut,
    ReviewArticleRequest,
)
from app.modules.articles.service import (
    create_article_comment,
    create_article_ex,
    create_category_ex,
    delete_article_comment,
    delete_category_ex,
    hard_delete_article,
    review_article,
    soft_delete_article,
    toggle_article_like,
    update_article_ex,
    update_category_ex,
)
from app.modules.auth.deps import CurrentUser, get_current_user
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from app.modules.rbac.service import check_owner

router = APIRouter(prefix="/articles", tags=["articles"])


@router.post("/{slug}/like", response_model=ApiResp[ArticleLikeStatus])
@respond
async def like_article(
    slug: str,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await toggle_article_like(db, slug, cur.id)


@router.post("/{slug}/comments", response_model=ApiResp[ArticleCommentOut])
@respond
async def add_article_comment(
    body: ArticleCommentCreate,
    slug: str,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ArticleCommentOut:
    # 返回序列化后的 schema，避免 @respond 直接 model_dump 无法处理 ORM 对象
    return ArticleCommentOut.model_validate(
        await create_article_comment(db, slug, cur.id, body.content, body.parent_id)
    )


@router.delete("/comments/{comment_id}", response_model=ApiResp[None])
@respond
async def remove_article_comment(
    comment_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    # 对象级权限：评论作者（user_id==cur.id）放行，或拥有 article_owner_comment_delete
    # 的 super_admin 代删。注意属主字段是 user_id（评论作者），非大文本的 author_id。
    await check_owner(
        db,
        cur,
        comment_id,
        ArticleComment,
        "user_id",
        Permission.article_owner_comment_delete,
    )
    # check_owner 已做对象级授权（属主或持 article.owner_comment_delete 的 super_admin），
    # service 层不再重复属主校验，故传 as_admin=True 跳过其内部 owner 检查。
    await delete_article_comment(db, comment_id, cur.id, as_admin=True)
    return None


# ——————— 写端点（均需 super_admin） ———————


@router.post("", response_model=ApiResp[ArticleDetail])
@respond
async def create_article(
    info: ArticleCreate,
    # 官方文章仅 super_admin 可写：RequirePermission 工厂已返回 Depends，勿再包一层。
    cur: CurrentUser = RequirePermission(Permission.articles_publish),
    db: AsyncSession = Depends(get_session),
) -> ArticleDetail:
    return await create_article_ex(db, info)


@router.patch("/{slug}", response_model=ApiResp[ArticleDetail])
@respond
async def patch_article(
    slug: str,
    patch: ArticleUpdate,
    cur: CurrentUser = RequirePermission(Permission.articles_publish),
    db: AsyncSession = Depends(get_session),
) -> ArticleDetail:
    return await update_article_ex(db, slug, patch, is_super=True)


@router.delete("/{slug}", response_model=ApiResp[dict[str, bool]])
@respond
async def soft_delete(
    slug: str,
    cur: CurrentUser = RequirePermission(Permission.articles_publish),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await soft_delete_article(db, slug)
    return {"ok": True}


@router.delete("/{slug}/hard", response_model=ApiResp[dict[str, bool]])
@respond
async def hard_delete(
    slug: str,
    cur: CurrentUser = RequirePermission(Permission.articles_review),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await hard_delete_article(db, slug)
    return {"ok": True}


@router.post("/{slug}/review", response_model=ApiResp[ArticleDetail])
@respond
async def review_article_endpoint(
    slug: str,
    body: ReviewArticleRequest,
    cur: CurrentUser = RequirePermission(Permission.articles_review),
    db: AsyncSession = Depends(get_session),
) -> ArticleDetail:
    return await review_article(db, slug, body.approve)


@router.post("/categories", response_model=ApiResp[CategoryOut])
@respond
async def add_category(
    info: CategoryCreate,
    cur: CurrentUser = RequirePermission(Permission.articles_category_manage),
    db: AsyncSession = Depends(get_session),
) -> CategoryOut:
    return await create_category_ex(db, info)


@router.patch("/categories/{category_id}", response_model=ApiResp[CategoryOut])
@respond
async def update_category(
    category_id: uuid.UUID,
    info: CategoryCreate,
    cur: CurrentUser = RequirePermission(Permission.articles_category_manage),
    db: AsyncSession = Depends(get_session),
) -> CategoryOut:
    return await update_category_ex(db, category_id, info)


@router.delete("/categories/{category_id}", response_model=ApiResp[dict[str, bool]])
@respond
async def delete_category(
    category_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.articles_category_manage),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await delete_category_ex(db, category_id)
    return {"ok": True}
