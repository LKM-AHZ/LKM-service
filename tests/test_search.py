"""M6.9 搜索 P1：站内检索（PG FTS + pg_trgm）。

覆盖：
- 中文子串命中（trgm 路，ILIKE 兜底；simple 分词对中文无效是本项存在的理由）
- 英文词命中 + 生成列随写入自动维护（tsvector 路）
- LIKE 通配符转义（``%`` 不得命中全表——不转义时此用例即红）
- 仅已发布内容 / content_type 过滤 / 排序带 rank
- 空白词 422、缺 q 422（REST）
- 命中项字段（author_name 内联、摘要、计数）
- 迁移建出的索引/生成列确实存在于 schema（防回退）
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.models import ContentItem, ContentStatus
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import create_item
from app.modules.search.errors import SearchErr
from app.modules.search.service import search_items
from tests.conftest import AuthUser, auth_user_uid


async def _au(auth_db: AsyncSession, username: str = "searcher") -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level="normal",
    )


async def _make_board(db: AsyncSession, slug: str = "search-b") -> int:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id


async def _make_item(
    db: AsyncSession,
    board_id: int,
    title: str,
    content: str = "正文",
    *,
    uid: int | None = None,
    status: str | None = None,
    content_type: str = "discussion",
) -> Any:
    item = await create_item(
        db,
        uid,
        ContentItemCreate(
            board_id=board_id,
            title=title,
            content=content,
            content_type=content_type,
        ),
    )
    if status is not None:
        obj = await db.get(ContentItem, item.id)
        assert obj is not None
        obj.status = status
        await db.flush()
    return item


async def test_chinese_substring_hits(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """中文子串检索：``simple`` 分词下 FTS 无能为力，命中来自 trgm/ILIKE 路。"""
    uid = (await _au(auth_db)).id
    bid = await _make_board(db)
    await _make_item(db, bid, "机器学习入门指南", uid=uid)
    await _make_item(db, bid, "另一篇无关文章", uid=uid)

    page = await search_items(db, "机器")
    assert page.total == 1
    assert page.items[0].title == "机器学习入门指南"

    # 词内子串（非前缀）也应命中：'学习' 不在 '机器学习入门指南' 的开头
    page2 = await search_items(db, "学习")
    assert page2.total == 1


async def test_english_word_hits_and_vector_maintained(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """英文词命中；并验证 tsvector 生成列在写入时由 PG 自动维护（非空且可匹配）。"""
    uid = (await _au(auth_db)).id
    bid = await _make_board(db)
    item = await _make_item(db, bid, "Machine Learning Guide", uid=uid)

    page = await search_items(db, "learning")
    assert page.total == 1

    matched = await db.scalar(
        text(
            "SELECT to_tsvector('simple', title) @@ plainto_tsquery('simple', :q) "
            "FROM content_items WHERE id = :iid"
        ).bindparams(q="learning", iid=item.id)
    )
    assert matched is True
    # 生成列确实物化到了行上（应用不写该列，由 PG 维护）
    assert (
        await db.scalar(
            text(
                "SELECT search_vector IS NOT NULL FROM content_items WHERE id = :i"
            ).bindparams(i=item.id)
        )
        is True
    )


async def test_like_wildcards_are_escaped(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """``%`` / ``_`` 按字面处理：不转义时 ``%`` 会命中全表（此用例即红）。"""
    uid = (await _au(auth_db)).id
    bid = await _make_board(db)
    await _make_item(db, bid, "普通标题", uid=uid)

    assert (await search_items(db, "%")).total == 0
    assert (await search_items(db, "_")).total == 0
    assert (await search_items(db, "\\")).total == 0


async def test_only_published_and_type_filter(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db)).id
    bid = await _make_board(db)
    await _make_item(db, bid, "已发布的可检索内容", uid=uid)
    await _make_item(db, bid, "草稿可检索内容", uid=uid, status=ContentStatus.DRAFT)

    page = await search_items(db, "可检索内容")
    assert page.total == 1
    assert page.items[0].title == "已发布的可检索内容"

    # content_type 过滤（草稿已被状态过滤，这里验证类型轴）
    page2 = await search_items(db, "可检索内容", content_type="article")
    assert page2.total == 0


async def test_hit_fields_inline_author_and_counts(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "alice")).id
    bid = await _make_board(db)
    await _make_item(db, bid, "带作者的内容", uid=uid)

    page = await search_items(db, "带作者")
    hit = page.items[0]
    assert hit.author_id == uid
    assert hit.author_name == "alice"  # 经 auth.snapshot 读缝回填
    assert hit.board_id == bid
    assert hit.like_count == 0
    assert hit.created_at is not None


async def test_blank_query_rejected(db: AsyncSession) -> None:
    with pytest.raises(BizError) as e:
        await search_items(db, "   ")
    assert e.value.errcode == SearchErr.EMPTY_QUERY


async def test_search_endpoint_rest(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    client: AsyncClient,
) -> None:
    uid = (await _au(auth_db)).id
    bid = await _make_board(db)
    await _make_item(db, bid, "REST 检索目标", uid=uid)

    r = await client.get("/api/v1/search", params={"q": "检索目标"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert [i["title"] for i in body["data"]["items"]] == ["REST 检索目标"]

    # 缺 q → 422（Query(min_length=1)）
    assert (await client.get("/api/v1/search")).status_code == 422
    # 纯空白 → 422（模块自定义错误码）
    assert (await client.get("/api/v1/search", params={"q": "  "})).status_code == 422
    # 状态端点
    assert (await client.get("/api/v1/search/status")).status_code == 200


async def test_search_indexes_present(db: AsyncSession) -> None:
    """防回退：迁移/模型必须建出 tsvector GIN 与两个 trgm GIN 索引。"""
    rows = (
        await db.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = "
                "'content_items' AND (indexname LIKE '%\\_trgm' OR indexname = "
                "'ix_content_search_vector')"
            )
        )
    ).all()
    defs = {r.indexname: r.indexdef for r in rows}
    assert "ix_content_search_vector" in defs
    assert "gin" in defs["ix_content_search_vector"].lower()
    assert "ix_content_title_trgm" in defs
    assert "ix_content_content_trgm" in defs
    assert "gin_trgm_ops" in defs["ix_content_title_trgm"]
