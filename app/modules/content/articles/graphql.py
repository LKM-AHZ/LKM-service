"""articles(官网文章) 只读 GraphQL。复用 service 读函数;已有缓存,resolver 不再套缓存。"""

import datetime
from typing import Any

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.core.err import BizError
from app.modules.content.articles.errors import ArticleErr
from app.modules.content.articles.schemas import ArticleDetail, ArticleListItem
from app.modules.content.articles.service import (
    get_about,
    get_article,
    list_articles,
    list_categories,
    list_tags,
    search_articles,
)

# GraphQL 分页边界：page/pageSize 是客户端可传的裸值，不夹紧会让 pageSize=0/负数直接落到
# SQL（负 offset/limit 报错、除零）或让超大 pageSize 整表拉取并污染 service 缓存键。
# 口径与 content/columns/graphql 的 _bounded_page_size 相同；业务模块间禁止相互 import
# （import-linter），故就地镜像一份。
_GRAPHQL_PAGE_SIZE = 20
_GRAPHQL_PAGE_MAX = 100


def _bounded_page_size(page_size: int | None) -> int:
    if page_size is None:
        return _GRAPHQL_PAGE_SIZE
    return max(1, min(page_size, _GRAPHQL_PAGE_MAX))


def _bounded_page(page: int) -> int:
    return max(1, page)


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


def _iso_or_none(dt: datetime.datetime | None) -> str | None:
    """把已有时刻格式化成 ISO 串（None 透传）——不取「当前时间」，故不叫 _now_iso。"""
    return dt.isoformat() if dt else None


def _base_item_fields(a: ArticleListItem | ArticleDetail) -> dict[str, Any]:
    """列表项与详情共用的字段集合。

    两处各自手抄同一批字段时，给 GraphArticleListItem 增删/改名会只在列表路径生效，
    详情路径静默漏字段；收敛到一处后两个入口一起变。
    """
    return {
        "slug": a.slug,
        "title": a.title,
        "description": a.description,
        "cover": a.cover,
        "categoryId": a.category_id,
        "categoryTitle": a.category_title,
        "published": _iso_or_none(a.published),
        "views": a.views,
        "likes": a.likes,
        "comments": a.comments,
    }


def _map_item(a: ArticleListItem | ArticleDetail) -> GraphArticleListItem:
    return GraphArticleListItem(**_base_item_fields(a))


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


@strawberry.type
class ArticlesQuery:
    @strawberry.field
    async def articles(
        self, info: Info, page: int = 1, pageSize: int = 20
    ) -> GraphArticlePage:
        db = _get_db(info)
        page_data = await list_articles(
            db, page=_bounded_page(page), limit=_bounded_page_size(pageSize)
        )
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
            **_base_item_fields(a),
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
        page_data = await search_articles(
            db, q, page=_bounded_page(page), limit=_bounded_page_size(pageSize)
        )
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
