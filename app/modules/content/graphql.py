"""content（统一内容源）与 boards（板块树）的只读 GraphQL schema 与解析器。

复用 REST 的 service 读函数 + content/service 的批量 map，避免双写分页/过滤逻辑；
GraphQL 字段 camelCase、时间 isoformat、id 用 int。boards 用于论坛分类轴。
"""

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.core.err import BizError
from app.modules.content.boards.service import list_boards
from app.modules.content.errors import ContentErr
from app.modules.content.models import ContentStatus
from app.modules.content.schemas import ContentCommentInfo, ContentItemInfo
from app.modules.content.service import (
    bump_item_view,
    get_item,
    get_item_by_slug,
    list_all_comments,
    list_comments,
    list_items,
)

# 公开 GraphQL 仅暴露该状态的内容（对齐 REST 列表的 PUBLISHED 过滤）
PUBLISHED_STATUS = ContentStatus.PUBLISHED.value

# ---- 节点类型 ----


@strawberry.type
class ContentAuthor:
    id: strawberry.ID
    name: str


@strawberry.type
class GraphContentItem:
    id: strawberry.ID
    contentType: str
    boardId: strawberry.ID
    authorId: strawberry.ID | None
    authorName: str
    publisher: str | None
    department: str | None
    columnId: strawberry.ID | None
    columnTitle: str
    qaQuestionId: strawberry.ID | None
    slug: str | None
    title: str
    excerpt: str
    summary: str | None
    cover: str | None
    keywords: list[str]
    content: str
    tags: list[str]
    status: str
    isPinned: bool
    isFeatured: bool
    viewCount: int
    likeCount: int
    commentCount: int
    bookmarkCount: int
    forwardCount: int
    readingTime: int
    createdAt: str
    publishedAt: str | None


@strawberry.type
class GraphContentComment:
    id: strawberry.ID
    contentId: strawberry.ID
    authorId: strawberry.ID
    authorName: str
    content: str
    floorNumber: int
    parentId: strawberry.ID | None
    likeCount: int
    createdAt: str
    children: list["GraphContentComment"] = strawberry.field(default_factory=list)


@strawberry.type
class GraphContentPage:
    items: list[GraphContentItem]
    total: int
    page: int
    pages: int


@strawberry.type
class GraphCommentPage:
    items: list[GraphContentComment]
    total: int
    page: int
    pages: int


@strawberry.type
class GraphBoard:
    id: strawberry.ID
    slug: str
    title: str
    description: str
    parentId: strawberry.ID | None
    ownerId: strawberry.ID | None
    status: str
    requireCertified: bool
    dailyPostLimit: int
    isPublic: bool
    createdAt: str


def _map_item(item: ContentItemInfo) -> GraphContentItem:
    return GraphContentItem(
        id=item.id,
        contentType=item.content_type,
        boardId=item.board_id,
        authorId=item.author_id,
        authorName=item.author_name,
        publisher=item.publisher,
        department=item.department,
        columnId=item.column_id,
        columnTitle=item.column_title,
        qaQuestionId=item.qa_question_id,
        slug=item.slug,
        title=item.title,
        excerpt=item.excerpt,
        summary=item.summary,
        cover=item.cover,
        keywords=item.keywords,
        content=item.content,
        tags=item.tags,
        status=item.status,
        isPinned=item.is_pinned,
        isFeatured=item.is_featured,
        viewCount=item.view_count,
        likeCount=item.like_count,
        commentCount=item.comment_count,
        bookmarkCount=item.bookmark_count,
        forwardCount=item.forward_count,
        readingTime=item.reading_time,
        createdAt=item.created_at.isoformat(),
        publishedAt=item.published_at.isoformat() if item.published_at else None,
    )


def _map_comment(c: ContentCommentInfo) -> GraphContentComment:
    return GraphContentComment(
        id=c.id,
        contentId=c.content_id,
        authorId=c.author_id,
        authorName=c.author_name,
        content=c.content,
        floorNumber=c.floor_number,
        parentId=c.parent_id,
        likeCount=c.like_count,
        createdAt=c.created_at.isoformat(),
    )


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


@strawberry.type
class ContentQuery:
    @strawberry.field
    async def contentItems(
        self,
        info: Info,
        page: int = 1,
        pageSize: int = 20,
        boardId: strawberry.ID | None = None,
        contentType: str | None = None,
    ) -> GraphContentPage:
        db = _get_db(info)
        page_data = await list_items(
            db, page=page, limit=pageSize, board_id=boardId, content_type=contentType
        )
        return GraphContentPage(
            items=[_map_item(i) for i in page_data.items],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )

    @strawberry.field
    async def contentItem(
        self, info: Info, id: strawberry.ID
    ) -> GraphContentItem | None:
        db = _get_db(info)
        try:
            item = await get_item(db, id, bump_view=False)
        except BizError as e:
            if e.errcode != ContentErr.CONTENT_NOT_FOUND:
                raise
            return None
        # 仅暴露已发布内容；草稿/待审/驳回(未发布)不向公开 GraphQL 泄露
        if item.status != PUBLISHED_STATUS:
            return None
        # 详情阅读计数增长：只在公开可见时原子 +1（草稿探测不计数）
        await bump_item_view(id)
        return _map_item(item)

    @strawberry.field
    async def contentItemBySlug(self, info: Info, slug: str) -> GraphContentItem | None:
        db = _get_db(info)
        try:
            item = await get_item_by_slug(db, slug)
        except BizError as e:
            if e.errcode != ContentErr.CONTENT_NOT_FOUND:
                raise
            return None
        if item.status != PUBLISHED_STATUS:
            return None
        # 与 contentItem 对齐：slug 同样是公开详情读路径，不计数会让按 slug 打开的阅读永远不涨
        await bump_item_view(item.id)
        return _map_item(item)

    @strawberry.field
    async def contentComments(
        self, info: Info, itemId: strawberry.ID, page: int = 1, pageSize: int = 20
    ) -> GraphCommentPage:
        db = _get_db(info)
        try:
            page_data = await list_comments(db, itemId, page=page, limit=pageSize)
        except BizError as e:
            if e.errcode != ContentErr.CONTENT_NOT_FOUND:
                raise
            return GraphCommentPage(items=[], total=0, page=page, pages=0)
        return GraphCommentPage(
            items=[_map_comment(c) for c in page_data.items],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )

    @strawberry.field
    async def boards(self, info: Info) -> list[GraphBoard]:
        db = _get_db(info)
        board_outs = await list_boards(db)
        return [
            GraphBoard(
                id=b.id,
                slug=b.slug,
                title=b.title,
                description=b.description,
                parentId=b.parent_id,
                ownerId=b.owner_id,
                status=b.status,
                requireCertified=b.require_certified,
                dailyPostLimit=b.daily_post_limit,
                isPublic=b.is_public,
                createdAt=b.created_at.isoformat(),
            )
            for b in board_outs
        ]

    @strawberry.field
    async def itemCommentTree(
        self, info: Info, itemId: strawberry.ID
    ) -> list[GraphContentComment]:
        """某内容的完整评论树（根为 parent_id is None 的楼层评论）。"""
        db = _get_db(info)
        try:
            comments = await list_all_comments(db, itemId)
        except BizError as e:
            if e.errcode != ContentErr.CONTENT_NOT_FOUND:
                raise
            return []
        by_parent: dict[int | None, list[ContentCommentInfo]] = {}
        for c in comments:
            by_parent.setdefault(c.parent_id, []).append(c)
        return [_map_comment_tree(c, by_parent) for c in by_parent.get(None, [])]


def _map_comment_tree(
    c: ContentCommentInfo,
    by_parent: dict[int | None, list[ContentCommentInfo]],
) -> GraphContentComment:
    """递归组装评论节点（当前评论 + 其子树）。"""
    node = _map_comment(c)
    node.children = [
        _map_comment_tree(child, by_parent) for child in by_parent.get(c.id, [])
    ]
    return node
