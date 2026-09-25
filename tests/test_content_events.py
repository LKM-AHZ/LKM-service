"""``content.*`` 领域事件（B1）：内容可见性变化 → outbox → ``content-index`` 订阅。

三层口径（``settings.pulsar_url`` 单测默认空 → ``enqueue_outbox`` fail-open 直接不入队）：

1) **真写点 + 真 outbox 行**：monkeypatch ``pulsar_url`` 非空后走 create_item /
   delete_item / publish_blog_item / _sync_question_content_item，查 ``OutboxMessage``
   的 routing_key 与 payload（事件是失效通知：只带 item_id 与 action）。
2) **不发的情形**：草稿态、``content_events_enabled`` 关闭、总线未配置。
3) **消费端契约**：直接调 ``search.tasks.apply_content_event``（worker 分派所跑的 handler），
   断言可重复执行（幂等由消费侧 upsert 承担）。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.messaging import RKEY_CONTENT_DELETED, RKEY_CONTENT_PUBLISHED
from app.db.outbox import OutboxMessage
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.models import QAQuestion
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import (
    _sync_question_content_item,
    create_item,
    delete_item,
    publish_blog_item,
)
from app.modules.search.tasks import apply_content_event
from tests.conftest import AuthUser, auth_user_uid

_BUS = "pulsar://content-events:6650"


async def _au(auth_db: AsyncSession, username: str = "alice") -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level="normal",
    )


async def _make_board(db: AsyncSession, slug: str) -> uuid.UUID:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id


async def _content_outbox(db: AsyncSession) -> list[OutboxMessage]:
    """本会话可见的 content.* 事件行（同事务内另会入队 points 事件，须过滤）。

    conftest 的会话 ``autoflush=False``，入队只 ``db.add``（由业务 commit 统一落库），
    故查询前须显式 flush 才看得到 pending 行。
    """
    await db.flush()
    rows = (await db.execute(select(OutboxMessage))).scalars().all()
    return [r for r in rows if r.routing_key.startswith("event.content.")]


async def test_published_item_enqueues_published_event(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", _BUS)
    au = await _au(auth_db)
    bid = await _make_board(db, "math")

    item = await create_item(
        db,
        au.id,
        ContentItemCreate(
            board_id=bid, title="黎曼猜想", content="从直觉理解", tags=["数学"]
        ),
    )
    assert item.status == "published"  # 讨论帖无审稿

    rows = await _content_outbox(db)
    assert len(rows) == 1
    assert rows[0].routing_key == RKEY_CONTENT_PUBLISHED
    assert rows[0].payload_json["fn"] == "apply_content_event"
    assert rows[0].payload_json["args"] == [str(item.id), "published"]


async def test_draft_item_enqueues_nothing(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", _BUS)
    au = await _au(auth_db, "drafter")
    bid = await _make_board(db, "news")

    item = await create_item(
        db,
        au.id,
        ContentItemCreate(
            board_id=bid,
            content_type="article",
            slug="draft-post",
            title="草稿",
            content="x",
            status="draft",
        ),
    )
    assert item.status == "draft"
    assert await _content_outbox(db) == []


async def test_delete_enqueues_deleted_event(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", _BUS)
    au = await _au(auth_db, "deleter")
    bid = await _make_board(db, "life")
    item = await create_item(
        db, au.id, ContentItemCreate(board_id=bid, title="待删", content="x")
    )

    await delete_item(db, item.id, au.id)
    rows = await _content_outbox(db)
    assert [r.routing_key for r in rows] == [
        RKEY_CONTENT_PUBLISHED,
        RKEY_CONTENT_DELETED,
    ]
    assert rows[-1].payload_json["args"] == [str(item.id), "deleted"]


async def test_blog_publish_enqueues_published_event(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", _BUS)
    au = await _au(auth_db, "blogger")
    bid = await _make_board(db, "blog")

    item_id = await publish_blog_item(
        db,
        au.id,
        board_id=bid,
        slug="first-post",
        title="博文",
        content="正文",
        summary=None,
        cover=None,
        tags=[],
    )
    rows = await _content_outbox(db)
    assert [r.routing_key for r in rows] == [RKEY_CONTENT_PUBLISHED]
    assert rows[0].payload_json["args"] == [str(item_id), "published"]


async def test_qa_sync_enqueues_published_event(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", _BUS)
    au = await _au(auth_db, "asker")
    q = QAQuestion(
        author_id=au.id,
        title="为什么天是蓝的",
        situation="好奇",
        content="求解",
        category="science",
        bounty_people=1,
        bounty_per_person=0,
        bounty_total=0,
        bounty_distributed=0,
        status="open",
    )
    db.add(q)
    await db.flush()

    await _sync_question_content_item(db, au.id, q)
    rows = await _content_outbox(db)
    assert [r.routing_key for r in rows] == [RKEY_CONTENT_PUBLISHED]


async def test_flag_off_disables_events(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", _BUS)
    monkeypatch.setattr(settings, "content_events_enabled", False)
    au = await _au(auth_db, "flagged")
    bid = await _make_board(db, "off")
    await create_item(
        db, au.id, ContentItemCreate(board_id=bid, title="不发", content="x")
    )
    assert await _content_outbox(db) == []


async def test_bus_off_disables_events(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", "")
    au = await _au(auth_db, "nobus")
    bid = await _make_board(db, "nobus")
    await create_item(
        db, au.id, ContentItemCreate(board_id=bid, title="不发", content="x")
    )
    assert await _content_outbox(db) == []


async def test_consumer_handler_is_repeatable() -> None:
    """消费侧 handler 幂等口径：同一事件重复执行不抛（真实索引写入在 B2，须 upsert）。"""
    await apply_content_event("11111111-1111-7111-8111-111111111111", "published")
    await apply_content_event("11111111-1111-7111-8111-111111111111", "published")
    await apply_content_event("11111111-1111-7111-8111-111111111111", "deleted")


def test_subscription_and_handler_are_wired() -> None:
    """接线契约：content-index 订阅存在，且其 payload.fn 在注册表里有 handler。

    少了任一环，事件会被静默「未知任务丢弃」（worker 只告警不报错），链路看着通、实际空转。
    """
    from app.core import task_registry
    from app.core.messaging import SUB_CONTENT_INDEX, SUBSCRIPTIONS
    from app.modules.content.events import CONTENT_EVENT_FN

    assert SUBSCRIPTIONS[SUB_CONTENT_INDEX.name] is SUB_CONTENT_INDEX

    task_registry.ensure_tasks_registered()
    handlers = task_registry.handlers_for(SUB_CONTENT_INDEX.name)
    assert CONTENT_EVENT_FN in handlers
