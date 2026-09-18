"""统一内容模型（content_items 收敛五套旧内容表）。

拆库后业务库(Base 无 users)不再有 User/Profile；content_items.author_id/owner_id 是
auth realm 稳定裸 uuid。凡"需要作者身份 / 读回作者名 author_name / 属主裁决/评论"的用例：
- ``_au(auth_db,...)`` 建 auth realm 用户并以裸 ``.id`` 给业务行；
- ``auth_seam_realm``：content service 的 create/list/get/comment 会跨 realm 回填
  author_name（display），须 seam 开 (指本测 auth_db) 才能读到——否则业务端直查 users 报错。
显示名回填 seam 已开启 → ``author_name == nickname``。

真双 PG(lkm/lkm_auth) schema-per-test 跑绿。

覆盖：
- 讨论帖 discussion 创建 / 列表按板过滤 / 详情 bump_view
- column_post 需挂 column_id，否则报 COLUMN_NOT_FOUND
- article 创建可带官方字段 publisher/department，slug 唯一校验
- 点赞幂等 + 取消点赞
- 评论楼层号递增 + 计数联动
- blog publish 落 content_items（blog_post）
- pinned 置顶排序 / view_count 自增 / 分页 / board 过滤（由 forum 测试迁移）
- 删帖（delete_item）后详情 CONTENT_NOT_FOUND
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.errors import ContentErr
from app.modules.content.models import Column, ContentItem
from app.modules.content.schemas import (
    ContentCommentCreate,
    ContentItemCreate,
)
from app.modules.content.service import (
    bump_item_view,
    create_comment,
    create_item,
    delete_item,
    get_item,
    like_item,
    list_comments,
    list_items,
    publish_blog_item,
    unlike_item,
)
from tests.conftest import AuthUser, auth_user_uid

# 合法 uuid7 形态的不存在内容 id。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


async def _au(
    auth_db: AsyncSession, username: str = "alice", nickname: str | None = None
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser（裸 .id 作业务 author）；nickname 即展示名。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=nickname or username,
        account_level="normal",
    )


async def _make_board(
    db: AsyncSession, slug: str, owner_id: uuid.UUID | None = None
) -> uuid.UUID:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), owner_id
        )
    ).id


async def _make_column(
    db: AsyncSession, owner_id: uuid.UUID, board_id: uuid.UUID
) -> uuid.UUID:
    col = Column(
        owner_id=owner_id,
        title="引力笔记",
        description="科普连载",
        slug="gravity",
        board_id=board_id,
    )
    db.add(col)
    await db.flush()
    return col.id


async def test_discussion_create_and_list(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    au = await _au(auth_db, "alice")
    uid = au.id
    bid = await _make_board(db, "math")
    item = await create_item(
        db,
        uid,
        ContentItemCreate(
            board_id=bid, title="黎曼猜想", content="从直觉理解", tags=["数学"]
        ),
    )
    assert item.content_type == "discussion"
    assert item.author_name == "alice"  # author_name 自 seam(auth realm nickname) 回填
    assert item.status == "published"  # 讨论帖无审稿

    page = await list_items(db, board_id=bid)
    assert page.total == 1
    assert page.items[0].title == "黎曼猜想"


async def test_column_post_requires_column(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "col_u")).id
    bid = await _make_board(db, "physics")
    with pytest.raises(BizError) as e:
        await create_item(
            db,
            uid,
            ContentItemCreate(
                board_id=bid,
                content_type="column_post",
                title="无专栏",
                content="x",
            ),
        )
    assert e.value.errcode == ContentErr.COLUMN_NOT_FOUND

    cid = await _make_column(db, uid, bid)
    item = await create_item(
        db,
        uid,
        ContentItemCreate(
            board_id=bid,
            content_type="column_post",
            column_id=cid,
            title="连载一",
            content="y",
        ),
    )
    assert item.column_id == cid


async def test_article_slug_unique_and_official_fields(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "art_u")).id
    bid = await _make_board(db, "official")
    await create_item(
        db,
        uid,
        ContentItemCreate(
            board_id=bid,
            content_type="article",
            slug="intro",
            title="入站指南",
            content="欢迎",
            publisher="LKM 官方",
            department="宣传部",
        ),
    )
    with pytest.raises(BizError) as e:
        await create_item(
            db,
            uid,
            ContentItemCreate(
                board_id=bid,
                content_type="article",
                slug="intro",
                title="重复",
                content="yy",
            ),
        )
    assert e.value.errcode == ContentErr.SLUG_TAKEN


async def test_like_idempotent_and_unlike(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "lk")).id
    bid = await _make_board(db, "physics")
    item = await create_item(
        db, uid, ContentItemCreate(board_id=bid, title="t", content="c")
    )
    n1 = await like_item(db, item.id, uid)
    n2 = await like_item(db, item.id, uid)  # 幂等
    assert n1 == 1 and n2 == 1
    n3 = await unlike_item(db, item.id, uid)
    assert n3 == 0


async def test_comment_floor_and_count(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "cc")).id
    bid = await _make_board(db, "cs")
    item = await create_item(
        db, uid, ContentItemCreate(board_id=bid, title="t", content="c")
    )
    c1 = await create_comment(db, item.id, uid, ContentCommentCreate(content="一楼"))
    c2 = await create_comment(db, item.id, uid, ContentCommentCreate(content="二楼"))
    assert c1.floor_number == 1 and c2.floor_number == 2

    detail = await get_item(db, item.id)
    assert detail.comment_count == 2

    page = await list_comments(db, item.id)
    assert page.total == 2


async def test_discussion_pinned_sorts_first(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """讨论帖 pinned 置顶排序（从 forum 迁移：pinned 在前）。"""
    uid = (await _au(auth_db, "pin")).id
    bid = await _make_board(db, "news")
    await create_item(
        db, uid, ContentItemCreate(board_id=bid, title="普通帖", content="a")
    )
    await create_item(
        db,
        uid,
        ContentItemCreate(board_id=bid, title="置顶帖", content="b", is_pinned=True),
    )

    page = await list_items(db, board_id=bid)

    assert page.total == 2
    assert page.items[0].title == "置顶帖"
    assert page.items[0].is_pinned is True
    assert page.items[1].title == "普通帖"


async def test_discussion_view_count_bumps(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """GET 详情两次 → view_count 递增 1（从 forum 迁移：view_count 自增）。"""
    uid = (await _au(auth_db, "view")).id
    bid = await _make_board(db, "math")
    item = await create_item(
        db, uid, ContentItemCreate(board_id=bid, title="t", content="c")
    )

    first = await get_item(db, item.id, bump_view=True)
    second = await get_item(db, item.id, bump_view=True)

    assert first.view_count == 1
    assert second.view_count == 2


async def test_get_nonexistent_item_raises(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """详情缺失 → CONTENT_NOT_FOUND（从 forum 迁移：不存在帖子报错）。"""
    uid = (await _au(auth_db, "nope")).id
    bid = await _make_board(db, "math")
    await create_item(db, uid, ContentItemCreate(board_id=bid, title="t", content="c"))

    with pytest.raises(BizError) as e:
        await get_item(db, _MISSING_ID)
    assert e.value.errcode == ContentErr.CONTENT_NOT_FOUND


async def test_bump_item_view_makes_write_session_commit(
    db: AsyncSession,
    auth_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    auth_seam_realm: None,
) -> None:
    """bump_item_view 自建独立写会话原子 +1 并 commit（GraphQL 只读会话不可写）。

    seam 注回绑定同一引擎的新会话：bump 内 commit/close 该新会话，不影响 fixture db。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    uid = (await _au(auth_db, "bump")).id
    bid = await _make_board(db, "math")
    item = await create_item(
        db, uid, ContentItemCreate(board_id=bid, title="t", content="c")
    )
    assert item.view_count == 0

    async def _new_session() -> AsyncSession:
        assert db.bind is not None
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        return factory()

    from app.modules.content import service as content_service

    monkeypatch.setattr(content_service, "_new_write_session", _new_session)
    await bump_item_view(item.id)

    # 从 DB 重新读取 ORM 行（同一 StaticPool 连接），应看到 bump 提交后的计数
    row = (
        (await db.execute(select(ContentItem).where(ContentItem.id == item.id)))
        .scalars()
        .one()
    )
    assert row.view_count == 1


async def test_delete_own_item(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """删除自己发的内容项 → 再查应 CONTENT_NOT_FOUND（从 forum 迁移：删帖）。"""
    uid = (await _au(auth_db, "del_own")).id
    bid = await _make_board(db, "math")
    item = await create_item(
        db, uid, ContentItemCreate(board_id=bid, title="t", content="c")
    )

    await delete_item(db, item.id, uid)

    with pytest.raises(BizError) as e:
        await get_item(db, item.id)
    assert e.value.errcode == ContentErr.CONTENT_NOT_FOUND


async def test_list_items_paginated(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """列表分页参数（从 forum 迁移：分页 total/pages）。"""
    uid = (await _au(auth_db, "pg")).id
    bid = await _make_board(db, "math")
    await create_item(db, uid, ContentItemCreate(board_id=bid, title="一", content="x"))
    await create_item(db, uid, ContentItemCreate(board_id=bid, title="二", content="y"))

    page = await list_items(db, page=1, limit=1)

    assert page.total == 2
    assert page.pages == 2
    assert len(page.items) == 1
    assert page.items[0].title == "二"  # 新帖在前（id desc）


async def test_list_items_filter_by_board(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """列表按板块过滤（从 forum 迁移：board 过滤）。"""
    uid = (await _au(auth_db, "fb")).id
    math_bid = await _make_board(db, "math")
    phys_bid = await _make_board(db, "physics")
    await create_item(
        db, uid, ContentItemCreate(board_id=math_bid, title="数学帖", content="x")
    )
    await create_item(
        db, uid, ContentItemCreate(board_id=phys_bid, title="物理帖", content="y")
    )

    page = await list_items(db, board_id=math_bid)

    assert page.total == 1
    assert page.items[0].board_id == math_bid
    assert page.items[0].title == "数学帖"


async def test_publish_blog_item_idempotent(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "blog_u")).id
    bid = await _make_board(db, "blog")
    cid1 = await publish_blog_item(
        db,
        uid,
        board_id=bid,
        slug="post-1",
        title="博客一",
        content="hello",
        summary="s",
        cover=None,
        tags=[],
    )
    cid2 = await publish_blog_item(
        db,
        uid,
        board_id=bid,
        slug="post-1",
        title="博客一v2",
        content="hello2",
        summary="s2",
        cover=None,
        tags=[],
    )
    assert cid1 == cid2  # 同 slug 幂等更新
    item = await db.get(ContentItem, cid1)
    assert item is not None
    assert item.status == "published"
    assert item.content_type == "blog_post"
