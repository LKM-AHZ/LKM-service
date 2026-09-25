import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.content import service as content_service
from app.modules.content.models import Board, ContentItem, ContentStatus, ContentType

# 合法的 uuid7 形态（第 3 段以 7 开头、第 4 段以 8 开头），用于"不存在"的 id 用例。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


async def _run(client: AsyncClient, query: str, variables: dict[str, Any]) -> Any:
    resp = await client.post("/graphql", json={"query": query, "variables": variables})
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


_INTROSPECTION = "query { __schema { queryType { name } } }"


async def should_expose_versioned_and_alias_endpoints(client: AsyncClient) -> None:
    """§2 多端点版本化：``/graphql/v1`` 与无版本别名 ``/graphql`` 同时可用且 schema 相同。

    别名保留是为了让存量前端（照旧打 ``/graphql``）零改动；带版本端点才是契约锚点，
    响应头 ``X-API-Version`` 让调用方确认命中的是哪个版本。
    """
    versioned = await client.post("/graphql/v1", json={"query": _INTROSPECTION})
    alias = await client.post("/graphql", json={"query": _INTROSPECTION})

    assert versioned.status_code == alias.status_code == 200
    assert versioned.json()["data"] == alias.json()["data"]
    assert versioned.headers["X-API-Version"] == "v1"
    # 别名路径不替调用方猜「现在等价于哪个版本」（见 core.middleware 的说明）
    assert "X-API-Version" not in alias.headers


async def should_register_only_declared_versions(client: AsyncClient) -> None:
    """未登记的版本端点不存在（访问即 404）——不静默回落到默认版本。"""
    resp = await client.post("/graphql/v99", json={"query": _INTROSPECTION})

    assert resp.status_code == 404


async def should_query_multiple_root_fields_in_one_operation(
    client: AsyncClient,
) -> None:
    """一次操作里查**多个根字段**必须成功（聚合读的基本形态）。

    回归的是 2026-09-26 真机验收发现的既有缺陷：graphql-core 并发执行同级字段，而所有
    resolver 共享同一个 AsyncSession → 两个都打 DB 的根字段并发即
    ``This session is provisioning a new connection; concurrent operations are not
    permitted`` → 500。蓝图把 GraphQL 定位为「聚合读」，聚合恰恰就是多根字段，只是前端
    目前只发单根才长期没暴露。
    """
    resp = await client.post(
        "/graphql/v1",
        json={"query": "query { contentItems(pageSize: 1) { total } boards { id } }"},
    )
    body = resp.json()

    assert resp.status_code == 200, body
    assert body.get("errors") is None, body
    assert body["data"]["contentItems"] is not None
    assert body["data"]["boards"] is not None


async def should_still_raise_for_unknown_root_field(client: AsyncClient) -> None:
    """串行化不能把「字段不存在」这类校验错误吞掉或变成 500。"""
    resp = await client.post(
        "/graphql/v1", json={"query": "query { noSuchRootField { id } }"}
    )

    assert resp.status_code == 200
    assert resp.json().get("errors"), resp.json()


@pytest.fixture(autouse=True)
async def _write_session_on_test_db(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """content 浏览计数的独立写会话默认走全局 ``new_session()``（连默认库，测试 schema
    无 content_items）。绑到本测 db 的 engine，令 ``bump_item_view`` 落同一测试 schema。
    （同 tests/test_content.py 的 ``_new_write_session`` patch 范式。）
    """

    async def _new_session():
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        return factory()

    monkeypatch.setattr(content_service, "_new_write_session", _new_session)


class TestContentGraphQL:
    """content 只读 GraphQL 契约测试（对齐前端 content.graphql.ts）。"""

    async def should_query_content_items(self, client: AsyncClient, db):
        # 需要后端 board + user 造数。此处用最小断言：查询空库返回空列表且无 errors。
        data = await _run(
            client,
            """
            query {
              contentItems(page: 1, pageSize: 10) {
                items { id title authorName contentType boardId }
                total page pages
              }
            }
            """,
            {},
        )
        assert data["contentItems"]["total"] >= 0
        assert isinstance(data["contentItems"]["items"], list)

    async def should_hide_unpublished_detail(self, client: AsyncClient, db):
        """contentItem 详情仅暴露 PUBLISHED；草稿/待审等未发布内容返回 null。"""
        b = Board(title="b", slug="b", description="", status="active")
        db.add(b)
        await db.flush()
        draft = ContentItem(
            content_type=ContentType.ARTICLE,
            board_id=b.id,
            title="草稿",
            content="未发布正文",
            status=ContentStatus.DRAFT,
        )
        published = ContentItem(
            content_type=ContentType.ARTICLE,
            board_id=b.id,
            title="已发布",
            content="公开正文",
            status=ContentStatus.PUBLISHED,
        )
        db.add_all([draft, published])
        await db.flush()

        data = await _run(
            client,
            "query($id: ID!) { contentItem(id: $id) { id title content } }",
            {"id": str(draft.id)},
        )
        assert data["contentItem"] is None

        data = await _run(
            client,
            "query($id: ID!) { contentItem(id: $id) { id title content } }",
            {"id": str(published.id)},
        )
        assert data["contentItem"] is not None
        assert data["contentItem"]["title"] == "已发布"


class TestArticlesGraphQL:
    """articles（官网文章）只读 GraphQL 契约测试。"""

    async def should_query_articles(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query {
              articles(page: 1, pageSize: 20) {
                items { slug title categoryTitle categoryId views }
                total page pages
              }
            }
            """,
            {},
        )
        assert isinstance(data["articles"]["items"], list)
        assert isinstance(data["articles"]["total"], int)

    async def should_search_articles(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query($q: String!) {
              searchArticles(q: $q, page: 1, pageSize: 20) {
                items { slug title }
                total
              }
            }
            """,
            {"q": "微积分"},
        )
        assert "searchArticles" in data

    async def should_query_article_tags(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query {
              articleTags { name articleCount }
            }
            """,
            {},
        )
        assert isinstance(data["articleTags"], list)
        for t in data["articleTags"]:
            assert "name" in t and "articleCount" in t

    async def should_query_about(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query {
              about { title description maintainer }
            }
            """,
            {},
        )
        assert data["about"]["title"]
        assert "description" in data["about"]
        assert "maintainer" in data["about"]


class TestFeedGraphQL:
    """feed(信息流) GraphQL 字段保真：follow+timeline 合并域在 app 层 merge_types 后仍在。

    M2.3 把 follow 与 timeline 并入单个 feed 域的 Query 类，随后被
    app/api/graphql.build_schema() 的 merge_types 聚合进单一"Query"。本测回归
    守卫：合并后的 app 级 schema 仍暴露 myFollowingUsers/myFollowingBoards(原
    FollowQuery) 与 timeline(原 TimelineQuery) 三字段，防止日后误删某 Query 类
    或漏掉 registry 登记导致字段静默丢失。仅作探针，不跑 resolver（空库查空即可）。
    """

    INTROSPECT_QUERY_FIELDS = """
    query {
      __schema {
        queryType {
          fields { name }
        }
      }
    }
    """

    async def should_expose_merged_feed_fields(self, client: AsyncClient, db):
        data = await _run(client, self.INTROSPECT_QUERY_FIELDS, {})
        field_names = {f["name"] for f in data["__schema"]["queryType"]["fields"]}
        # 合并前各自存在于 follow/timeline Query，合并后必须在 app 级单一 Query 上保真。
        assert {"myFollowingUsers", "myFollowingBoards", "timeline"} <= field_names

    async def should_query_timeline_without_error(self, client: AsyncClient, db):
        # 真实 field-selection 探测：即使空库也要干净返回（无 id/errors 混入）。
        data = await _run(
            client,
            """
            query {
              timeline(mode: "follow") {
                items { id url }
                nextCursor
              }
            }
            """,
            {},
        )
        assert data["timeline"]["items"] == []
        assert data["timeline"]["nextCursor"] is None


class TestColumnsGraphQL:
    """columns(专栏) 只读 GraphQL 契约测试（对齐前端 column.graphql.ts）。"""

    async def should_query_columns(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query {
              columns(page: 1) {
                items { id title slug authorName boardId }
                total page pages
              }
            }
            """,
            {},
        )
        assert isinstance(data["columns"]["items"], list)

    async def should_query_column_posts(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query($id: ID!) {
              columnPosts(columnId: $id, page: 1) {
                items { id title summary viewCount }
                total
              }
            }
            """,
            {"id": str(_MISSING_ID)},
        )
        assert "columnPosts" in data


class TestBlogGraphQL:
    """blog(博客 Series/Git 文件) 只读 GraphQL 契约测试。"""

    async def should_query_series(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query {
              blogSeries {
                items { id title ownerId starCount isStarred }
                total page pages
              }
            }
            """,
            {},
        )
        assert isinstance(data["blogSeries"]["items"], list)

    async def should_query_series_detail(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query($id: ID!) {
              blogSeriesDetail(seriesId: $id) {
                id title
                fileTree { name type children { name type } }
              }
            }
            """,
            {"id": str(_MISSING_ID)},
        )
        # 不存在的 series 应返回 null（resolver 捕获异常）
        assert data["blogSeriesDetail"] is None

    async def should_query_blog_file_content_not_found(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query($id: ID!, $filepath: String!) {
              blogFileContent(seriesId: $id, filepath: $filepath) {
                filepath content
              }
            }
            """,
            {"id": str(_MISSING_ID), "filepath": "README.md"},
        )
        # 不存在的 series 应返回 null（resolver 捕获 SERIES_NOT_FOUND）
        assert data["blogFileContent"] is None


class TestProjectsGraphQL:
    """projects(项目) 只读 GraphQL 契约测试（复用 projects.service）。"""

    async def should_query_projects(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query {
              projects {
                items { id title summary members { id displayName roleInProject } }
              }
            }
            """,
            {},
        )
        assert isinstance(data["projects"]["items"], list)

    async def should_query_project(self, client: AsyncClient, db):
        data = await _run(
            client,
            """
            query($id: ID!) {
              project(projectId: $id) { id title summary }
            }
            """,
            {"id": str(_MISSING_ID)},
        )
        # 不存在的 project 应返回 null（resolver 捕获 NOT_FOUND）
        assert data["project"] is None
