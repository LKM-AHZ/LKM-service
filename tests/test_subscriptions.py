"""统一内容订阅（board/tag/column）与单帖 watch。

覆盖：
- 订阅 CRUD / 幂等墓碑 / tag 归一化 / 非法目标拒绝；
- 板块订阅统一进 ``content_subscriptions`` 后 feed 仍能读（模拟 0006 迁移后的行）；
- 新帖按 board/tag/column fanout，``notify_level='none'`` 与已退订不再提醒；
- 收藏 / 评论自动 watch，watch 帖的后续评论/点赞通知 watcher；
- 自己发的内容/自己评论不通知自己；同类通知按现有聚合窗口合并。
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.columns.models import Column
from app.modules.comment.models import ContentComment
from app.modules.comment.schemas import ContentCommentCreate
from app.modules.comment.service import create_comment
from app.modules.content.models import Board, ContentItem, ContentStatus, ContentType
from app.modules.feed.service import get_timeline
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.models import ContentSubscription, PostWatch
from app.modules.interaction.service import (
    add_favorite,
    get_followed_board_ids,
    list_subscriptions,
    list_watches,
    subscribe_content,
    unsubscribe_content,
    watch_post,
)
from app.modules.notification import tasks as notif_tasks
from app.modules.notification.models import Notification
from app.modules.notification.service import NotificationType
from core.err import BizError
from tests.conftest import AuthUser, auth_user_uid


async def _au(auth_db: AsyncSession, name: str) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=name,
        email=f"{name}@example.com",
        nickname=name,
        account_level="normal",
    )


def _hdr(user: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.token}"}


async def _board(db: AsyncSession, slug: str) -> uuid.UUID:
    board = Board(slug=slug, title=slug, description="", status="active")
    db.add(board)
    await db.flush()
    return board.id


async def _column(db: AsyncSession, owner_id: uuid.UUID, slug: str) -> uuid.UUID:
    column = Column(owner_id=owner_id, title=slug, description="", slug=slug)
    db.add(column)
    await db.flush()
    return column.id


async def _item(
    db: AsyncSession,
    board_id: uuid.UUID,
    author_id: uuid.UUID,
    title: str = "帖子",
    *,
    tags: list[str] | None = None,
    column_id: uuid.UUID | None = None,
    status: ContentStatus = ContentStatus.PUBLISHED,
) -> uuid.UUID:
    item = ContentItem(
        content_type=(
            ContentType.COLUMN_POST if column_id is not None else ContentType.DISCUSSION
        ),
        board_id=board_id,
        author_id=author_id,
        column_id=column_id,
        title=title,
        content="正文",
        tags=json.dumps(tags or [], ensure_ascii=False),
        status=status,
    )
    db.add(item)
    await db.flush()
    return item.id


async def _comment(
    db: AsyncSession, item_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    floor = (
        await db.scalar(
            select(func.count())
            .select_from(ContentComment)
            .where(ContentComment.content_id == item_id)
        )
        or 0
    ) + 1
    row = ContentComment(
        content_id=item_id, user_id=user_id, content="评论", floor_number=floor
    )
    db.add(row)
    await db.flush()
    return row.id


async def _notifications(
    db: AsyncSession, user_id: uuid.UUID, kind: str
) -> list[Notification]:
    return list(
        (
            await db.execute(
                select(Notification)
                .where(
                    Notification.user_id == user_id,
                    Notification.type == kind,
                )
                .order_by(Notification.id)
            )
        )
        .scalars()
        .all()
    )


async def _sub_rows(
    db: AsyncSession, user_id: uuid.UUID
) -> list[ContentSubscription]:
    return list(
        (
            await db.execute(
                select(ContentSubscription).where(
                    ContentSubscription.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.fixture
def patched_handler(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """通知消费者指向本测会话并假化推送/展示名读缝（与 test_notification 同款）。"""

    async def _new_session() -> AsyncSession:
        return db

    async def _publish(*_args: object, **_kwargs: object) -> None:
        return None

    class _FakeSnap:
        display_name = "触发者"

    async def _snapshot(_db: AsyncSession, *, user_id: uuid.UUID) -> _FakeSnap:
        return _FakeSnap()

    monkeypatch.setattr(notif_tasks, "new_session", _new_session)
    monkeypatch.setattr(notif_tasks, "publish_notification", _publish)
    monkeypatch.setattr(notif_tasks, "get_user_snapshot", _snapshot)


class TestSubscriptionCrud:
    async def test_subscribe_idempotent_and_tombstone(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        me = await _au(auth_db, "sub_me")
        bid = await _board(db, "sub-board")

        assert await subscribe_content(db, me.id, "board", str(bid)) is True
        assert await subscribe_content(db, me.id, "board", str(bid)) is False
        assert len(await _sub_rows(db, me.id)) == 1

        assert await unsubscribe_content(db, me.id, "board", str(bid)) is True
        assert await unsubscribe_content(db, me.id, "board", str(bid)) is False
        assert await get_followed_board_ids(db, me.id) == []

        # 重订阅复活同一行（软删墓碑），不产生第二行
        assert await subscribe_content(db, me.id, "board", str(bid)) is True
        assert len(await _sub_rows(db, me.id)) == 1
        assert await get_followed_board_ids(db, me.id) == [bid]

    async def test_tag_normalized_trim_lower(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        me = await _au(auth_db, "tag_me")
        await subscribe_content(db, me.id, "tag", "  Math  ")

        items = await list_subscriptions(db, me.id)
        assert [(i.target_type, i.target_id) for i in items] == [("tag", "math")]

        # 同一标签的不同写法仍是同一条订阅
        await subscribe_content(db, me.id, "tag", "MATH")
        rows = await _sub_rows(db, me.id)
        assert len(rows) == 1 and rows[0].target_id == "math"

    async def test_invalid_targets_rejected(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        me = await _au(auth_db, "bad_me")
        with pytest.raises(BizError) as err:
            await subscribe_content(db, me.id, "user", str(uuid.uuid4()))
        assert err.value.errcode == InteractionErr.INVALID_TARGET_TYPE

        with pytest.raises(BizError) as err2:
            await subscribe_content(db, me.id, "board", "not-a-uuid")
        assert err2.value.errcode == InteractionErr.INVALID_TARGET_TYPE

        with pytest.raises(BizError) as err3:
            await subscribe_content(db, me.id, "board", str(uuid.uuid4()))
        assert err3.value.errcode == InteractionErr.SUBSCRIPTION_TARGET_NOT_FOUND

        with pytest.raises(BizError) as err4:
            await subscribe_content(db, me.id, "tag", "   ")
        assert err4.value.errcode == InteractionErr.INVALID_TARGET_TYPE

    async def test_migrated_board_row_feeds_timeline(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        """模拟 0006 搬迁后的行：feed 的 board 过滤仍读统一订阅表。"""
        me = await _au(auth_db, "mig_me")
        author = await _au(auth_db, "mig_author")
        bid = await _board(db, "mig-board")
        await _item(db, bid, author.id, "搬迁后的版块帖")
        # 迁移后的等价行（target_type='board'）
        db.add(
            ContentSubscription(
                user_id=me.id, target_type="board", target_id=str(bid)
            )
        )
        await db.flush()

        assert await get_followed_board_ids(db, me.id) == [bid]
        feed = await get_timeline(
            db, user_id=me.id, mode="follow", cursor=None, limit=20
        )
        titles = [i.title for i in feed.items if i.item_type == "discussion"]
        assert "搬迁后的版块帖" in titles

    async def test_http_subscription_routes(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        client: AsyncClient,
    ) -> None:
        me = await _au(auth_db, "http_me")
        bid = await _board(db, "http-board")
        body = {"target_type": "board", "target_id": str(bid)}

        resp = await client.post(
            "/api/v1/interaction/subscriptions", json=body, headers=_hdr(me)
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["subscribed"] is True

        listing = await client.get(
            "/api/v1/interaction/me/subscriptions", headers=_hdr(me)
        )
        assert listing.status_code == 200
        assert listing.json()["data"]["items"][0]["target_id"] == str(bid)

        removed = await client.request(
            "DELETE",
            "/api/v1/interaction/subscriptions",
            json=body,
            headers=_hdr(me),
        )
        assert removed.status_code == 200
        assert removed.json()["data"]["subscribed"] is False


class TestFanout:
    async def test_new_post_notifies_board_tag_column_subscribers(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "fan_author")
        board_sub = await _au(auth_db, "fan_board")
        tag_sub = await _au(auth_db, "fan_tag")
        col_sub = await _au(auth_db, "fan_col")
        bid = await _board(db, "fan-board")
        cid = await _column(db, author.id, "fan-column")
        await subscribe_content(db, board_sub.id, "board", str(bid))
        await subscribe_content(db, tag_sub.id, "tag", "Math")
        await subscribe_content(db, col_sub.id, "column", str(cid))

        item_id = await _item(
            db, bid, author.id, "新帖", tags=["math"], column_id=cid
        )
        await notif_tasks.notify_from_point_event(
            author.id, "post", f"item:{item_id}"
        )

        for subscriber in (board_sub, tag_sub, col_sub):
            rows = await _notifications(
                db, subscriber.id, NotificationType.SUBSCRIPTION_NEW_POST
            )
            assert len(rows) == 1
            assert rows[0].target_id == item_id
            assert rows[0].actor_id == author.id
            assert rows[0].payload["title"] == "新帖"

        # 作者自己不是订阅者 → 不通知
        assert (
            await _notifications(
                db, author.id, NotificationType.SUBSCRIPTION_NEW_POST
            )
            == []
        )

    async def test_draft_and_unsubscribed_do_not_notify(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "fan2_author")
        sub = await _au(auth_db, "fan2_sub")
        bid = await _board(db, "fan2-board")
        await subscribe_content(db, sub.id, "board", str(bid))

        draft = await _item(
            db, bid, author.id, "草稿", status=ContentStatus.DRAFT
        )
        await notif_tasks.notify_from_point_event(
            author.id, "post", f"item:{draft}"
        )
        assert (
            await _notifications(db, sub.id, NotificationType.SUBSCRIPTION_NEW_POST)
            == []
        )

        await unsubscribe_content(db, sub.id, "board", str(bid))
        published = await _item(db, bid, author.id, "退订后的帖")
        await notif_tasks.notify_from_point_event(
            author.id, "post", f"item:{published}"
        )
        assert (
            await _notifications(db, sub.id, NotificationType.SUBSCRIPTION_NEW_POST)
            == []
        )

    async def test_notify_level_none_suppresses(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "none_author")
        sub = await _au(auth_db, "none_sub")
        bid = await _board(db, "none-board")
        await subscribe_content(db, sub.id, "board", str(bid), notify_level="none")

        # 只关提醒，不取消关注：feed 受众面仍包含该版块
        assert await get_followed_board_ids(db, sub.id) == [bid]

        item_id = await _item(db, bid, author.id, "静音订阅")
        await notif_tasks.notify_from_point_event(
            author.id, "post", f"item:{item_id}"
        )
        assert (
            await _notifications(db, sub.id, NotificationType.SUBSCRIPTION_NEW_POST)
            == []
        )


class TestPostWatch:
    async def test_favorite_and_comment_auto_watch(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        author = await _au(auth_db, "w_author")
        fan = await _au(auth_db, "w_fan")
        commenter = await _au(auth_db, "w_commenter")
        bid = await _board(db, "w-board")
        item_id = await _item(db, bid, author.id, "自动 watch")

        await add_favorite(db, fan.id, item_id)
        await create_comment(
            db, item_id, commenter.id, ContentCommentCreate(content="先评一句")
        )

        watched = {
            row.user_id
            for row in (
                await db.execute(
                    select(PostWatch).where(PostWatch.content_id == item_id)
                )
            )
            .scalars()
            .all()
        }
        assert watched == {fan.id, commenter.id}
        page = await list_watches(db, fan.id)
        assert page.total == 1 and page.items[0].content_id == item_id

    async def test_watch_comment_notifies_watchers(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "wn_author")
        watcher = await _au(auth_db, "wn_watcher")
        commenter = await _au(auth_db, "wn_commenter")
        bid = await _board(db, "wn-board")
        item_id = await _item(db, bid, author.id, "watch 帖")
        await watch_post(db, watcher.id, item_id)
        comment_id = await _comment(db, item_id, commenter.id)

        await notif_tasks.notify_from_point_event(
            commenter.id, "comment", f"comment:{comment_id}"
        )

        rows = await _notifications(
            db, watcher.id, NotificationType.POST_WATCH_ACTIVITY
        )
        assert len(rows) == 1
        assert rows[0].target_id == item_id
        assert rows[0].actor_id == commenter.id
        assert rows[0].payload["comment_id"] == str(comment_id)
        # 评论者自己不是 watcher 目标；作者只拿 content_commented
        assert (
            await _notifications(
                db, commenter.id, NotificationType.POST_WATCH_ACTIVITY
            )
            == []
        )

    async def test_watch_none_suppresses(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "wn2_author")
        watcher = await _au(auth_db, "wn2_watcher")
        actor = await _au(auth_db, "wn2_actor")
        bid = await _board(db, "wn2-board")
        item_id = await _item(db, bid, author.id, "静音 watch")
        await watch_post(db, watcher.id, item_id, notify_level="none")

        await notif_tasks.notify_from_point_event(actor.id, "like", f"item:{item_id}")

        assert (
            await _notifications(
                db, watcher.id, NotificationType.POST_WATCH_ACTIVITY
            )
            == []
        )

    async def test_self_activity_not_notified(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "self_author")
        bid = await _board(db, "self-board")
        item_id = await _item(db, bid, author.id, "自己的帖")
        await watch_post(db, author.id, item_id)

        # 自己给自己的帖点赞、自己评论：都不通知自己
        await notif_tasks.notify_from_point_event(
            author.id, "like", f"item:{item_id}"
        )
        my_comment = await _comment(db, item_id, author.id)
        await notif_tasks.notify_from_point_event(
            author.id, "comment", f"comment:{my_comment}"
        )

        assert await _notifications(
            db, author.id, NotificationType.POST_WATCH_ACTIVITY
        ) == []
        assert await _notifications(
            db, author.id, NotificationType.CONTENT_LIKED
        ) == []
        assert await _notifications(
            db, author.id, NotificationType.CONTENT_COMMENTED
        ) == []

    async def test_watch_activity_aggregates(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        patched_handler: None,
    ) -> None:
        author = await _au(auth_db, "agg_author")
        watcher = await _au(auth_db, "agg_watcher")
        actor = await _au(auth_db, "agg_actor")
        bid = await _board(db, "agg-board")
        item_id = await _item(db, bid, author.id, "聚合 watch")
        await watch_post(db, watcher.id, item_id)

        await notif_tasks.notify_from_point_event(actor.id, "like", f"item:{item_id}")
        await notif_tasks.notify_from_point_event(actor.id, "like", f"item:{item_id}")

        rows = await _notifications(
            db, watcher.id, NotificationType.POST_WATCH_ACTIVITY
        )
        assert len(rows) == 1, "同 actor+target 的未读 watch 通知应合并"
        assert rows[0].payload["count"] == 2
