"""articles(官网文章) 只读 GraphQL。复用 service 读函数;已有缓存,resolver 不再套缓存。"""

import datetime

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.core.err import BizError
from app.modules.articles.errors import ArticleErr
from app.modules.articles.schemas import ArticleDetail, ArticleListItem
from app.modules.articles.service import (
    get_about,
    get_article,
    list_articles,
    list_categories,
    list_tags,
    search_articles,
)


@strawberry.type
class GraphArticleListItem:
    slug: str
    title: str
    description: str | None
    cover: str | None
    categoryId: strawberry.ID
    categoryTitle: str
    published: str | None
    views: int
    likes: int
    comments: int


@strawberry.type
class GraphArticleDetail(GraphArticleListItem):
    bookmarks: int
    department: str | None
    publisher: str | None
    content: str
    readingTime: int
    keywords: list[str]
    tags: list[str]


@strawberry.type
class GraphArticleCategory:
    slug: str
    name: str
    articleCount: int


@strawberry.type
class GraphArticleTag:
    name: str
    articleCount: int


@strawberry.type
class GraphAbout:
    title: str
    description: str
    maintainer: str


@strawberry.type
class GraphArticlePage:
    items: list[GraphArticleListItem]
    total: int
    page: int
    pages: int


def _now_iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _map_item(a: ArticleListItem | ArticleDetail) -> GraphArticleListItem:
    return GraphArticleListItem(
        slug=a.slug,
        title=a.title,
        description=a.description,
        cover=a.cover,
        categoryId=a.category_id,
        categoryTitle=a.category_title,
        published=_now_iso(a.published),
        views=a.views,
        likes=a.likes,
        comments=a.comments,
    )


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


@strawberry.type
class ArticlesQuery:
    @strawberry.field
    async def articles(
        self, info: Info, page: int = 1, pageSize: int = 20
    ) -> GraphArticlePage:
        db = _get_db(info)
        page_data = await list_articles(db, page=page, limit=pageSize)
        return GraphArticlePage(
            items=[_map_item(a) for a in page_data.items],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )

    @strawberry.field
    async def article(self, info: Info, slug: str) -> GraphArticleDetail | None:
        db = _get_db(info)
        try:
            a = await get_article(db, slug)
        except BizError as e:
            if e.errcode != ArticleErr.NOT_FOUND:
                raise
            return None
        return GraphArticleDetail(
            slug=a.slug,
            title=a.title,
            description=a.description,
            cover=a.cover,
            categoryId=a.category_id,
            categoryTitle=a.category_title,
            published=_now_iso(a.published),
            views=a.views,
            likes=a.likes,
            comments=a.comments,
            bookmarks=a.bookmarks,
            department=a.department,
            publisher=a.publisher,
            content=a.content,
            readingTime=a.reading_time,
            keywords=a.keywords,
            tags=a.tags,
        )

    @strawberry.field
    async def articleCategories(self, info: Info) -> list[GraphArticleCategory]:
        db = _get_db(info)
        cats = await list_categories(db)
        return [
            GraphArticleCategory(slug=c.slug, name=c.name, articleCount=c.article_count)
            for c in cats
        ]

    @strawberry.field
    async def searchArticles(
        self, info: Info, q: str, page: int = 1, pageSize: int = 20
    ) -> GraphArticlePage:
        db = _get_db(info)
        page_data = await search_articles(db, q, page=page, limit=pageSize)
        return GraphArticlePage(
            items=[_map_item(a) for a in page_data.items],
            total=page_data.total,
            page=page_data.page,
            pages=page_data.pages,
        )

    @strawberry.field
    async def articleTags(self, info: Info) -> list[GraphArticleTag]:
        db = _get_db(info)
        tags = await list_tags(db)
        return [
            GraphArticleTag(
                name=t.get("name", ""), articleCount=t.get("article_count", 0)
            )
            for t in tags
        ]

    @strawberry.field
    async def about(self) -> GraphAbout:
        data = await get_about()
        return GraphAbout(
            title=data["title"],
            description=data["description"],
            maintainer=data["maintainer"],
        )
