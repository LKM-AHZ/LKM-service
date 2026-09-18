"""columns(专栏) 只读 GraphQL。复用 service;作者/专栏名由 service 已填充,不另起批量。"""

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.core.err import BizError
from app.modules.content.column_models import ColumnPostStatus, ColumnStatus
from app.modules.content.columns.errors import ColumnErr
from app.modules.content.columns.schemas import ColumnInfo, ColumnPostInfo
from app.modules.content.columns.service import (
    get_column,
    get_column_by_slug,
    list_columns,
    list_posts,
)


@strawberry.type
class GraphColumn:
    id: strawberry.ID
    ownerId: strawberry.ID
    applicationId: strawberry.ID | None
    title: str
    description: str
    slug: str | None
    coverUrl: str | None
    authorName: str | None
    authorTitle: str | None
    authorBio: str | None
    avatarUrl: str | None
    isVerified: bool
    followerCount: int
    likeCount: int
    subscribeCount: int
    articleCount: int
    tags: list[str]
    badges: list[str]
    boardId: strawberry.ID | None
    status: str


@strawberry.type
class GraphColumnPost:
    id: strawberry.ID
    columnId: strawberry.ID
    authorId: strawberry.ID
    title: str
    summary: str | None
    content: str
    coverImage: str | None
    viewCount: int
    likeCount: int
    commentCount: int
    status: str
    publishedAt: str | None


@strawberry.type
class GraphColumnPage:
    items: list[GraphColumn]
    total: int
    page: int
    pages: int


@strawberry.type
class GraphColumnPostPage:
    items: list[GraphColumnPost]
    total: int
    page: int
    pages: int


def _s(v: ColumnStatus | ColumnPostStatus) -> str:
    return str(v.value) if hasattr(v, "value") else str(v)


# 公开 GraphQL 仅暴露这些状态（专栏/文章在公共查询里不展示草稿/归档）
# 用字符串比较，兼容 service 返回经 Pydantic 转为枚举或原始 str 两种形态。
_VISIBLE_COLUMN = {ColumnStatus.ACTIVE.value}
_VISIBLE_POST = {ColumnPostStatus.PUBLISHED.value}


def _visible_col(c: ColumnInfo) -> bool:
    return _s(c.status) in _VISIBLE_COLUMN


def _visible_post(p: ColumnPostInfo) -> bool:
    return _s(p.status) in _VISIBLE_POST


# GraphQL 分页上限：前端不传 pageSize 时不可给 service 传 None（否则全表拉取）
_GRAPHQL_PAGE_SIZE = 20
_GRAPHQL_PAGE_MAX = 100


def _bounded_page_size(page_size: int | None) -> int:
    """pageSize None→默认；超上限截断，杜绝一次拉全表。"""
    if page_size is None:
        return _GRAPHQL_PAGE_SIZE
    return max(1, min(page_size, _GRAPHQL_PAGE_MAX))


def _map_col(c: ColumnInfo) -> GraphColumn:
    return GraphColumn(
        id=c.id,
        ownerId=c.owner_id,
        applicationId=c.application_id,
        title=c.title,
        description=c.description,
        slug=c.slug,
        coverUrl=c.cover_url,
        authorName=c.author_name,
        authorTitle=c.author_title,
        authorBio=c.author_bio,
        avatarUrl=c.avatar_url,
        isVerified=c.is_verified,
        followerCount=c.follower_count,
        likeCount=c.like_count,
        subscribeCount=c.subscribe_count,
        articleCount=c.article_count,
        tags=c.tags,
        badges=c.badges,
        boardId=c.board_id,
        status=_s(c.status),
    )


def _map_post(p: ColumnPostInfo) -> GraphColumnPost:
    return GraphColumnPost(
        id=p.id,
        columnId=p.column_id,
        authorId=p.author_id,
        title=p.title,
        summary=p.summary,
        content=p.content,
        coverImage=p.cover_image,
        viewCount=p.view_count,
        likeCount=p.like_count,
        commentCount=p.comment_count,
        status=_s(p.status),
        publishedAt=p.published_at.isoformat() if p.published_at else None,
    )


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


@strawberry.type
class ColumnsQuery:
    @strawberry.field
    async def columns(
        self, info: Info, page: int = 1, pageSize: int | None = None
    ) -> GraphColumnPage:
        db = _get_db(info)
        page_data = await list_columns(
            db, page=page, limit=_bounded_page_size(pageSize)
        )
        return GraphColumnPage(
            # 仅暴露 ACTIVE 专栏
            items=[_map_col(c) for c in page_data.items if _visible_col(c)],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )

    @strawberry.field
    async def column(self, info: Info, id: strawberry.ID) -> GraphColumn | None:
        db = _get_db(info)
        try:
            c = await get_column(db, id)
        except BizError as e:
            if e.errcode != ColumnErr.NOT_FOUND:
                raise
            return None
        if not _visible_col(c):
            return None
        return _map_col(c)

    @strawberry.field
    async def columnBySlug(self, info: Info, slug: str) -> GraphColumn | None:
        db = _get_db(info)
        try:
            c = await get_column_by_slug(db, slug)
        except BizError as e:
            if e.errcode != ColumnErr.NOT_FOUND:
                raise
            return None
        if not _visible_col(c):
            return None
        return _map_col(c)

    @strawberry.field
    async def columnPosts(
        self,
        info: Info,
        columnId: strawberry.ID,
        page: int = 1,
        pageSize: int | None = None,
    ) -> GraphColumnPostPage:
        db = _get_db(info)
        try:
            page_data = await list_posts(
                db, columnId, page=page, limit=_bounded_page_size(pageSize)
            )
        except BizError as e:
            if e.errcode != ColumnErr.NOT_FOUND:
                raise
            return GraphColumnPostPage(items=[], total=0, page=page, pages=0)
        # 仅暴露 PUBLISHED 文章；草稿/归档不向公开 GraphQL 泄露
        return GraphColumnPostPage(
            items=[_map_post(p) for p in page_data.items if _visible_post(p)],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )
