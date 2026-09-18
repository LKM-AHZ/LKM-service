"""blog(博客 Series/Git 文件) 只读 GraphQL。复用 service;首版不引入用户态,is_starred 走 None。"""

from typing import Any

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.core.err import BizError
from app.modules.auth.schemas import ProfileInfo
from app.modules.blog.errors import BlogErr
from app.modules.blog.schemas import (
    BlogCommentInfo,
    BlogSeriesDetail,
    BlogSeriesInfo,
)
from app.modules.blog.service import (
    get_file_content,
    get_series,
    list_comments,
    list_series,
)


@strawberry.type
class GraphFileTreeNode:
    name: str
    type: str
    children: list["GraphFileTreeNode"] | None = None


@strawberry.type
class GraphSeriesCommentAuthor:
    id: strawberry.ID
    name: str | None


@strawberry.type
class GraphSeriesComment:
    id: strawberry.ID
    userId: strawberry.ID
    seriesId: strawberry.ID
    content: str
    parentId: strawberry.ID | None
    createdAt: str
    author: GraphSeriesCommentAuthor | None = None
    replies: list["GraphSeriesComment"] = strawberry.field(default_factory=list)


@strawberry.type
class GraphBlogSeries:
    id: strawberry.ID
    ownerId: strawberry.ID
    title: str
    description: str | None
    coverUrl: str | None
    repoName: str
    status: str
    starCount: int
    isStarred: bool


@strawberry.type
class GraphBlogSeriesDetail(GraphBlogSeries):
    fileTree: list[GraphFileTreeNode] | None


@strawberry.type
class GraphSeriesPage:
    items: list[GraphBlogSeries]
    total: int
    page: int
    pages: int


@strawberry.type
class GraphFileContent:
    filepath: str
    content: str


def _map_series(s: BlogSeriesInfo | BlogSeriesDetail) -> GraphBlogSeries:
    return GraphBlogSeries(
        id=s.id,
        ownerId=s.owner_id,
        title=s.title,
        description=s.description,
        coverUrl=s.cover_url,
        repoName=s.repo_name,
        status=str(s.status.value) if hasattr(s.status, "value") else s.status,
        starCount=s.star_count,
        isStarred=s.is_starred,
    )


def _map_file_tree(nodes: list[dict[str, Any]]) -> list[GraphFileTreeNode]:
    return [
        GraphFileTreeNode(
            name=n["name"],
            type=n["type"],
            children=_map_file_tree(n["children"]) if n.get("children") else None,
        )
        for n in nodes
    ]


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


def _comment_author(c: BlogCommentInfo) -> GraphSeriesCommentAuthor | None:
    # service 已批量填充 c.profile（ProfileInfo），直接复用，避免丢弃既有数据。
    p: ProfileInfo | None = c.profile
    name = p.nickname if (p and p.nickname) else ""
    return GraphSeriesCommentAuthor(id=c.user_id, name=name or None)


def _map_comment(c: BlogCommentInfo) -> GraphSeriesComment:
    return GraphSeriesComment(
        id=c.id,
        userId=c.user_id,
        seriesId=c.series_id,
        content=c.content,
        parentId=c.parent_id,
        createdAt=c.created_at.isoformat(),
        author=_comment_author(c),
        replies=[_map_comment(r) for r in c.replies],
    )


@strawberry.type
class BlogQuery:
    @strawberry.field
    async def blogSeries(
        self, info: Info, page: int = 1, pageSize: int | None = None
    ) -> GraphSeriesPage:
        db = _get_db(info)
        page_data = await list_series(
            db, current_user_id=None, page=page, limit=pageSize
        )
        return GraphSeriesPage(
            items=[_map_series(s) for s in page_data.items],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )

    @strawberry.field
    async def blogSeriesDetail(
        self, info: Info, seriesId: strawberry.ID
    ) -> GraphBlogSeriesDetail | None:
        db = _get_db(info)
        try:
            s = await get_series(db, seriesId, current_user_id=None)
        except BizError as e:
            if e.errcode != BlogErr.SERIES_NOT_FOUND:
                raise
            return None
        base = _map_series(s)
        tree: list[GraphFileTreeNode] | None = None
        if s.file_tree:
            tree = _map_file_tree(s.file_tree)
        return GraphBlogSeriesDetail(
            id=base.id,
            ownerId=base.ownerId,
            title=base.title,
            description=base.description,
            coverUrl=base.coverUrl,
            repoName=base.repoName,
            status=base.status,
            starCount=base.starCount,
            isStarred=base.isStarred,
            fileTree=tree,
        )

    @strawberry.field
    async def blogSeriesComments(
        self, info: Info, seriesId: strawberry.ID
    ) -> list[GraphSeriesComment]:
        db = _get_db(info)
        try:
            comments = await list_comments(db, seriesId)
        except BizError as e:
            if e.errcode != BlogErr.SERIES_NOT_FOUND:
                raise
            return []
        # 树形已由 service 拼装到 top-level;service 返回顶层 roots(每条的 replies/已含 profile)。
        return [_map_comment(c) for c in comments]

    @strawberry.field
    async def blogFileContent(
        self, info: Info, seriesId: strawberry.ID, filepath: str
    ) -> GraphFileContent | None:
        db = _get_db(info)
        try:
            d = await get_file_content(db, seriesId, filepath)
        except BizError as e:
            if e.errcode != BlogErr.SERIES_NOT_FOUND:
                raise
            return None
        return GraphFileContent(filepath=d["filepath"], content=d["content"])
