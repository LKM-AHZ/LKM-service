"""notification 域（M6.8）：站内信产生（含聚合/偏好门控）、已读、偏好、token、
以及消费者侧「事件 → 通知 + WS 推送」链路。
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.content.models import (
    Board,
    ContentComment,
    ContentItem,
    ContentStatus,
    ContentType,
)
from app.modules.notification import tasks as notif_tasks
from app.modules.notification.errors import NotificationErr
from app.modules.notification.models import (
    Notification,
    NotificationPreference,
    NotificationToken,
)
from app.modules.notification.service import (
    NotificationType,
    create_notification,
    delete_token,
    is_type_enabled,
    list_notifications,
    list_preferences,
    mark_read,
    register_token,
    set_preferences,
    unread_count,
)
from tests.conftest import auth_user_uid

# 聚合目标 id（Notification.target_id 为 uuid 列）；同一测试内多次复用同一值。
_TARGET_ID = uuid.UUID("00000000-0000-7000-8000-000000000007")


async def _mk_user(auth_db: AsyncSession, username: str) -> uuid.UUID:
    return (
        await auth_user_uid(
            auth_db,
            username=username,
            email=f"{username}@x.test",
            nickname=username,
            account_level="normal",
            with_token=False,
        )
    ).id


async def _mk_item(
    db: AsyncSession, author_id: uuid.UUID, title: str = "帖子"
) -> uuid.UUID:
    board = Board(slug=f"b-{title}", title="B", description="", status="active")
    db.add(board)
    await db.flush()
    item = ContentItem(
        content_type=ContentType.DISCUSSION,
        board_id=board.id,
        author_id=author_id,
        title=title,
        content="正文",
        status=ContentStatus.PUBLISHED,
    )
    db.add(item)
    await db.flush()
    return item.id


async def _mk_comment(
    db: AsyncSession,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    parent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    floor = (
        await db.scalar(
            select(func.count())
            .select_from(ContentComment)
            .where(ContentComment.content_id == item_id)
        )
        or 0
    ) + 1
    c = ContentComment(
        content_id=item_id,
        user_id=user_id,
        content="评论",
        floor_number=floor,
        parent_id=parent_id,
    )
    db.add(c)
    await db.flush()
    return c.id


async def _rows(db: AsyncSession, user_id: uuid.UUID) -> list[Notification]:
    return list(
        (
            await db.execute(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.id)
            )
        )
        .scalars()
        .all()
    )


class _FakeSnap:
    display_name = "触发者"


@pytest.fixture
def patched_handler(monkeypatch: pytest.MonkeyPatch, db: AsyncSession):
    """把 handler 的 new_session 指向本测会话、记录 WS 推送、并假化展示名读缝。

    展示名读缝走 auth 库，业务测试 schema 无 ``users`` 表，故统一假化；其降级行为由
    ``test_actor_name_failure_degrades`` 单独覆盖。
    """
    pushed: list[tuple] = []

    async def _new_session() -> AsyncSession:
        return db

    async def _fake_publish(user_id, payload, *, event_id=None, version=None) -> None:
        pushed.append((user_id, payload, event_id, version))

    async def _fake_snapshot(_db: AsyncSession, *, user_id: uuid.UUID) -> _FakeSnap:
        return _FakeSnap()

    monkeypatch.setattr(notif_tasks, "new_session", _new_session)
    monkeypatch.setattr(notif_tasks, "publish_notification", _fake_publish)
    monkeypatch.setattr(notif_tasks, "get_user_snapshot", _fake_snapshot)
    return pushed


class TestService:
    async def test_create_and_list(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db, "n1")
        await create_notification(
            db, user_id=uid, type=NotificationType.CONTENT_LIKED, payload={"title": "t"}
        )
        await create_notification(
            db, user_id=uid, type=NotificationType.CONTENT_COMMENTED
        )

        page = await list_notifications(db, uid, page=1, limit=10)

        assert page.total == 2
        assert page.items[0].type == NotificationType.CONTENT_COMMENTED  # id 倒序
        assert await unread_count(db, uid) == 2

        only_unread = await list_notifications(db, uid, unread_only=True)
        assert only_unread.total == 2

    async def test_aggregate_same_actor_and_target(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db, "n2")
        actor = await _mk_user(auth_db, "n2b")

        await create_notification(
            db,
            user_id=uid,
            type=NotificationType.CONTENT_LIKED,
            actor_id=actor,
            target_id=_TARGET_ID,
            payload={"title": "t"},
        )
        merged = await create_notification(
            db,
            user_id=uid,
            type=NotificationType.CONTENT_LIKED,
            actor_id=actor,
            target_id=_TARGET_ID,
            payload={"title": "t"},
        )

        rows = await _rows(db, uid)
        assert len(rows) == 1, "同类同目标的未读通知应合并"
        assert rows[0].payload["count"] == 2
        assert merged.id == rows[0].id

    async def test_aggregate_skips_read_rows(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db, "n3")
        actor = await _mk_user(auth_db, "n3b")
        await create_notification(
            db,
            user_id=uid,
            type=NotificationType.CONTENT_LIKED,
            actor_id=actor,
            target_id=_TARGET_ID,
            payload={},
        )
        await mark_read(db, uid, ids=[], all_=True)

        await create_notification(
            db,
            user_id=uid,
            type=NotificationType.CONTENT_LIKED,
            actor_id=actor,
            target_id=_TARGET_ID,
            payload={},
        )

        assert len(await _rows(db, uid)) == 2, "已读通知不再参与聚合"

    async def test_mark_read_scoped_to_own_rows(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        mine = await _mk_user(auth_db, "n4")
        other = await _mk_user(auth_db, "n4b")
        row = await create_notification(db, user_id=other, type="content_liked")

        assert await mark_read(db, mine, ids=[row.id]) == 0, "不能标记别人的通知已读"
        assert await mark_read(db, other, ids=[row.id]) == 1
        assert await unread_count(db, other) == 0

    async def test_preferences_default_on_and_update(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db, "n5")

        defaults = await list_preferences(db, uid)
        assert all(p.enabled for p in defaults)
        assert {p.type for p in defaults} == {
            NotificationType.CONTENT_LIKED,
            NotificationType.CONTENT_COMMENTED,
            NotificationType.COMMENT_REPLIED,
        }
        assert await is_type_enabled(db, uid, NotificationType.CONTENT_LIKED)

        updated = await set_preferences(
            db, uid, [(NotificationType.CONTENT_LIKED, False)]
        )

        assert not next(
            p for p in updated if p.type == NotificationType.CONTENT_LIKED
        ).enabled
        assert await is_type_enabled(db, uid, NotificationType.CONTENT_LIKED) is False
        stored = (
            await db.execute(
                select(func.count())
                .select_from(NotificationPreference)
                .where(NotificationPreference.user_id == uid)
            )
        ).scalar()
        assert stored == 1, "局部更新只写一行"

    async def test_unknown_preference_type_rejected(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db, "n6")
        with pytest.raises(BizError) as err:
            await set_preferences(db, uid, [("bogus", False)])
        assert err.value.errcode == NotificationErr.INVALID_TYPE

    async def test_token_register_idempotent_and_delete(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db, "n7")
        await register_token(db, uid, "tok-1", "web")
        again = await register_token(db, uid, "tok-1", "android")

        rows = (
            (
                await db.execute(
                    select(NotificationToken).where(NotificationToken.user_id == uid)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "同 (user_id, token) 只一行"
        assert again.platform == "android"
        assert await delete_token(db, uid, "tok-1") == 1
        assert await delete_token(db, uid, "tok-1") == 0


class TestConsumeEvents:
    async def test_like_notifies_content_author(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        author = await _mk_user(auth_db, "a1")
        actor = await _mk_user(auth_db, "a2")
        item_id = await _mk_item(db, author, "被赞的帖")

        await notif_tasks.notify_from_point_event(actor, "like", f"item:{item_id}")

        rows = await _rows(db, author)
        assert len(rows) == 1
        row = rows[0]
        assert row.type == NotificationType.CONTENT_LIKED
        assert row.actor_id == actor
        assert row.target_id == item_id
        assert row.payload["title"] == "被赞的帖"
        assert row.payload["url"] == f"/content/posts/{item_id}"
        assert row.payload["actor_name"] == "触发者"
        assert len(patched_handler) == 1
        user_id, payload, event_id, version = patched_handler[0]
        assert user_id == author
        assert payload["event"] == "notification_created"
        assert payload["notification_id"] == str(row.id)
        assert event_id == f"notification:{row.id}"  # 同一通知重推 payload 一致
        assert version == row.id.int  # WS version 用 uuid 的 128 位整数

    async def test_self_like_not_notified(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        author = await _mk_user(auth_db, "a3")
        item_id = await _mk_item(db, author)

        await notif_tasks.notify_from_point_event(author, "like", f"item:{item_id}")

        assert await _rows(db, author) == []
        assert patched_handler == []

    async def test_comment_notifies_author_and_parent_author(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        author = await _mk_user(auth_db, "a4")
        commenter = await _mk_user(auth_db, "a5")
        replier = await _mk_user(auth_db, "a6")
        item_id = await _mk_item(db, author)
        parent = await _mk_comment(db, item_id, commenter)
        reply = await _mk_comment(db, item_id, replier, parent_id=parent)

        await notif_tasks.notify_from_point_event(
            replier, "comment", f"comment:{reply}"
        )

        assert [(r.type, r.actor_id) for r in await _rows(db, author)] == [
            (NotificationType.CONTENT_COMMENTED, replier)
        ]
        assert [(r.type, r.actor_id) for r in await _rows(db, commenter)] == [
            (NotificationType.COMMENT_REPLIED, replier)
        ]
        assert {c[0] for c in patched_handler} == {author, commenter}

    async def test_unresolvable_events_are_noop(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        actor = await _mk_user(auth_db, "a7")
        item_id = await _mk_item(db, actor)

        # post/competition 等非目标事件、以及无法解析的 ref 一律不产通知
        await notif_tasks.notify_from_point_event(actor, "post", f"item:{item_id}")
        await notif_tasks.notify_from_point_event(actor, "competition", "cert:9")
        await notif_tasks.notify_from_point_event(actor, "like", "garbage")

        assert patched_handler == []

    async def test_duplicate_like_aggregates(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        author = await _mk_user(auth_db, "a8")
        actor = await _mk_user(auth_db, "a9")
        item_id = await _mk_item(db, author)

        await notif_tasks.notify_from_point_event(actor, "like", f"item:{item_id}")
        await notif_tasks.notify_from_point_event(actor, "like", f"item:{item_id}")

        rows = await _rows(db, author)
        assert len(rows) == 1, "同一人对同一目标的重复点赞合并为一条"
        assert rows[0].payload["count"] == 2

    async def test_disabled_preference_stores_without_push(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        author = await _mk_user(auth_db, "a10")
        actor = await _mk_user(auth_db, "a11")
        item_id = await _mk_item(db, author)
        await set_preferences(db, author, [(NotificationType.CONTENT_LIKED, False)])

        await notif_tasks.notify_from_point_event(actor, "like", f"item:{item_id}")

        assert len(await _rows(db, author)) == 1, "偏好关闭仍落库（站内信不丢）"
        assert patched_handler == [], "偏好关闭时不实时推送"

    async def test_missing_item_is_noop(
        self, db: AsyncSession, auth_db: AsyncSession, patched_handler: list
    ) -> None:
        actor = await _mk_user(auth_db, "a12")

        await notif_tasks.notify_from_point_event(actor, "like", "item:999999")

        assert patched_handler == []

    async def test_actor_name_failure_degrades(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """展示名读缝炸掉时：不丢通知，actor_name 降级为空串。"""
        author = await _mk_user(auth_db, "a13")
        actor = await _mk_user(auth_db, "a14")
        item_id = await _mk_item(db, author)

        async def _new_session() -> AsyncSession:
            return db

        async def _boom(_db: AsyncSession, *, user_id: uuid.UUID) -> None:
            raise RuntimeError("snapshot down")

        monkeypatch.setattr(notif_tasks, "new_session", _new_session)
        monkeypatch.setattr(notif_tasks, "get_user_snapshot", _boom)

        await notif_tasks.notify_from_point_event(actor, "like", f"item:{item_id}")

        rows = await _rows(db, author)
        assert len(rows) == 1, "读名失败不得阻断通知生成"
        assert rows[0].payload["actor_name"] == ""
