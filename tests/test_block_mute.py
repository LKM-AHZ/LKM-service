"""Block / Mute：双向 block、单向 mute，以及 feed/search/notification/互动面的过滤。

- block 存单向行但语义双向：任一方向存在 block，两人互相不可见、互相不可互动；
- mute 单向：只有发起方看不到被静音者，对方仍可正常互动；
- block 时双向软删关注关系，unblock 不自动恢复；
- 树洞是匿名身份，不接入账号 block/mute（见 interaction.service 的说明）。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.comment.models import ContentComment
from app.modules.comment.schemas import ContentCommentCreate
from app.modules.comment.service import create_comment, like_comment
from app.modules.content.models import Board, ContentItem, ContentStatus, ContentType
from app.modules.content.service import like_item
from app.modules.feed.models import FeedItemMaterialized
from app.modules.feed.service import get_timeline
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.models import UserFollow, UserRelationship
from app.modules.interaction.service import (
    block_user,
    follow_user,
    get_blocked_ids,
    get_hidden_author_ids,
    is_blocked_either_direction,
    list_blocked_ids,
    list_muted_ids,
    mute_user,
    relationship_state,
    unblock_user,
    unmute_user,
)
from app.modules.notification.service import create_notification, list_notifications
from app.modules.search.service import search_items
from core.err import BizError, CommonErr
from tests.conftest import AuthUser, auth_user_uid


async def _au(auth_db: AsyncSession, name: str) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=name,
        email=f"{name}@example.com",
        nickname=name,
        account_level="normal",
    )


async def _board(db: AsyncSession, slug: str) -> uuid.UUID:
    board = Board(slug=slug, title=slug, description="", status="active")
    db.add(board)
    await db.flush()
    return board.id


async def _item(
    db: AsyncSession, board_id: uuid.UUID, author_id: uuid.UUID | None, title: str
) -> uuid.UUID:
    item = ContentItem(
        content_type=ContentType.DISCUSSION,
        board_id=board_id,
        author_id=author_id,
        title=title,
        content="正文",
        status=ContentStatus.PUBLISHED,
    )
    db.add(item)
    await db.flush()
    return item.id


def _auth_header(user: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.token}"}


async def _relationship_count(
    db: AsyncSession, user_id: uuid.UUID, target_id: uuid.UUID, kind: str
) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(UserRelationship)
            .where(
                UserRelationship.user_id == user_id,
                UserRelationship.target_id == target_id,
                UserRelationship.kind == kind,
            )
        )
    ) or 0


class TestBlockMuteSemantics:
    async def test_block_is_bidirectional_and_idempotent(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "blocker")
        b = await _au(auth_db, "blocked")

        await block_user(db, a.id, b.id)
        await block_user(db, a.id, b.id)  # 幂等

        assert await _relationship_count(db, a.id, b.id, "block") == 1
        assert await is_blocked_either_direction(db, a.id, b.id) is True
        # 双向：反向判定同样成立，但只存 a→b 一行
        assert await is_blocked_either_direction(db, b.id, a.id) is True
        assert await _relationship_count(db, b.id, a.id, "block") == 0
        assert set(await get_blocked_ids(db, b.id)) == {a.id}
        assert set(await get_blocked_ids(db, a.id)) == {b.id}
        assert await list_blocked_ids(db, a.id) == [b.id]
        assert await list_blocked_ids(db, b.id) == []

        await unblock_user(db, a.id, b.id)
        assert await is_blocked_either_direction(db, a.id, b.id) is False

    async def test_mute_is_one_directional_and_idempotent(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "muter")
        b = await _au(auth_db, "muted")

        await mute_user(db, a.id, b.id)
        await mute_user(db, a.id, b.id)

        assert await _relationship_count(db, a.id, b.id, "mute") == 1
        assert await list_muted_ids(db, a.id) == [b.id]
        # 单向：被静音者不隐藏静音者
        assert await list_muted_ids(db, b.id) == []
        assert set(await get_hidden_author_ids(db, a.id)) == {b.id}
        assert await get_hidden_author_ids(db, b.id) == []

        await unmute_user(db, a.id, b.id)
        assert await list_muted_ids(db, a.id) == []

    async def test_self_and_missing_target_rejected(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "self")
        with pytest.raises(BizError) as self_err:
            await block_user(db, a.id, a.id)
        assert self_err.value.errcode == InteractionErr.CANNOT_RELATE_SELF

        with pytest.raises(BizError) as mute_self:
            await mute_user(db, a.id, a.id)
        assert mute_self.value.errcode == InteractionErr.CANNOT_RELATE_SELF

        missing = uuid.uuid4()
        with pytest.raises(BizError) as block_missing:
            await block_user(db, a.id, missing)
        assert block_missing.value.errcode == InteractionErr.RELATION_TARGET_NOT_FOUND
        with pytest.raises(BizError) as mute_missing:
            await mute_user(db, a.id, missing)
        assert mute_missing.value.errcode == InteractionErr.RELATION_TARGET_NOT_FOUND

    async def test_block_clears_follows_both_directions(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "follow_a")
        b = await _au(auth_db, "follow_b")
        await follow_user(db, a.id, b.id)
        await follow_user(db, b.id, a.id)

        await block_user(db, a.id, b.id)

        rows = (
            await db.execute(
                select(UserFollow).where(
                    (UserFollow.follower_id == a.id)
                    | (UserFollow.follower_id == b.id)
                )
            )
        ).scalars().all()
        assert len(rows) == 2
        assert all(r.deleted_at is not None for r in rows)

        # unblock 不恢复关注
        await unblock_user(db, a.id, b.id)
        still = (
            await db.execute(
                select(UserFollow).where(
                    UserFollow.follower_id == a.id,
                    UserFollow.following_id == b.id,
                )
            )
        ).scalar_one()
        assert still.deleted_at is not None

    async def test_relationship_state(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "state_a")
        b = await _au(auth_db, "state_b")
        await block_user(db, a.id, b.id)
        await mute_user(db, a.id, b.id)

        assert await relationship_state(db, a.id, b.id) == (True, False, True)
        assert await relationship_state(db, b.id, a.id) == (False, True, False)

    async def test_follow_rejected_when_blocked(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "fol_a")
        b = await _au(auth_db, "fol_b")
        await block_user(db, b.id, a.id)  # 被关注者屏蔽了关注者

        with pytest.raises(BizError) as err:
            await follow_user(db, a.id, b.id)
        assert err.value.errcode == InteractionErr.BLOCKED


class TestFeedFiltering:
    async def test_materialized_follow_filters_blocked_and_muted(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        blocked = await _au(auth_db, "feed_blocked")
        muted = await _au(auth_db, "feed_muted")
        reader = await _au(auth_db, "feed_reader")
        bid = await _board(db, "bm-feed")
        await _item(db, bid, blocked.id, "被屏蔽的帖子")
        await _item(db, bid, muted.id, "被静音的帖子")

        # 先建立关注并回填物化行，再 block/mute：验证读路径过滤（而非关注清理碰巧生效）
        await follow_user(db, reader.id, blocked.id)
        await follow_user(db, reader.id, muted.id)
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FeedItemMaterialized)
                .where(FeedItemMaterialized.user_id == reader.id)
            )
        ) == 2

        await block_user(db, reader.id, blocked.id)
        # block 清掉了关注；此时再关注一个非屏蔽作者，确保物化路径仍有候选
        normal = await _au(auth_db, "feed_normal")
        await _item(db, bid, normal.id, "正常帖子")
        await follow_user(db, reader.id, normal.id)
        await mute_user(db, reader.id, muted.id)

        resp = await get_timeline(
            db, user_id=reader.id, mode="follow", cursor=None, limit=20
        )
        assert [i.title for i in resp.items] == ["正常帖子"]

    async def test_hot_realtime_filters_blocked(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        blocked = await _au(auth_db, "hot_blocked")
        normal = await _au(auth_db, "hot_normal")
        reader = await _au(auth_db, "hot_reader")
        bid = await _board(db, "bm-hot")
        await _item(db, bid, blocked.id, "被屏蔽的热帖")
        await _item(db, bid, normal.id, "正常热帖")

        await block_user(db, reader.id, blocked.id)
        resp = await get_timeline(
            db, user_id=reader.id, mode="hot", cursor=None, limit=20
        )
        titles = [i.title for i in resp.items]
        assert "被屏蔽的热帖" not in titles
        assert "正常热帖" in titles

        # 匿名不过滤
        anon = await get_timeline(db, user_id=None, mode="hot", cursor=None, limit=20)
        assert any(i.title == "被屏蔽的热帖" for i in anon.items)


class TestSearchFiltering:
    async def test_pg_search_filters_blocked_and_muted(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        blocked = await _au(auth_db, "search_blocked")
        muted = await _au(auth_db, "search_muted")
        normal = await _au(auth_db, "search_normal")
        viewer = await _au(auth_db, "search_viewer")
        bid = await _board(db, "bm-search")
        await _item(db, bid, blocked.id, "黎曼猜想-屏蔽")
        await _item(db, bid, muted.id, "黎曼猜想-静音")
        await _item(db, bid, normal.id, "黎曼猜想-正常")

        await block_user(db, viewer.id, blocked.id)
        await mute_user(db, viewer.id, muted.id)

        page = await search_items(db, "黎曼猜想", viewer_id=viewer.id)
        assert [h.title for h in page.items] == ["黎曼猜想-正常"]
        # 匿名不过滤
        anon = await search_items(db, "黎曼猜想")
        assert len(anon.items) == 3

    async def test_engine_backfill_filters_hidden_authors(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.modules.search import service as search_service
        from tests.fakes import FakeSearchEngine

        blocked = await _au(auth_db, "eng_blocked")
        normal = await _au(auth_db, "eng_normal")
        viewer = await _au(auth_db, "eng_viewer")
        bid = await _board(db, "bm-engine")
        blocked_item = await _item(db, bid, blocked.id, "引擎命中-屏蔽")
        normal_item = await _item(db, bid, normal.id, "引擎命中-正常")

        engine = FakeSearchEngine()
        for item_id in (blocked_item, normal_item):
            engine.docs[str(item_id)] = {"id": str(item_id), "title": "引擎命中"}
        monkeypatch.setattr(search_service, "get_engine", lambda: engine)
        await block_user(db, viewer.id, blocked.id)

        page = await search_service.search_items(db, "引擎命中", viewer_id=viewer.id)
        assert [h.id for h in page.items] == [normal_item]


class TestNotificationSuppression:
    async def test_blocked_both_directions_suppressed(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "notif_a")
        b = await _au(auth_db, "notif_b")

        # 收件人屏蔽了 actor
        await block_user(db, a.id, b.id)
        assert (
            await create_notification(
                db, user_id=a.id, type="content_liked", actor_id=b.id
            )
            is None
        )
        # actor 屏蔽了收件人（反向同样抑制）
        assert (
            await create_notification(
                db, user_id=b.id, type="content_liked", actor_id=a.id
            )
            is None
        )
        page = await list_notifications(db, a.id)
        assert page.total == 0

    async def test_mute_only_suppresses_for_muting_user(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _au(auth_db, "mute_notif_a")
        b = await _au(auth_db, "mute_notif_b")
        await mute_user(db, a.id, b.id)

        # a 静音了 b：b 发给 a 的通知被抑制
        assert (
            await create_notification(
                db, user_id=a.id, type="content_liked", actor_id=b.id
            )
            is None
        )
        # 反向不受影响：a 发给 b 的通知照常落库
        row = await create_notification(
            db, user_id=b.id, type="content_liked", actor_id=a.id
        )
        assert row is not None
        assert (await list_notifications(db, b.id)).total == 1


class TestInteractionRejection:
    async def test_block_denies_comment_like_and_post_like(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        author = await _au(auth_db, "inter_author")
        viewer = await _au(auth_db, "inter_viewer")
        bid = await _board(db, "bm-inter")
        item_id = await _item(db, bid, author.id, "互动目标")
        await block_user(db, author.id, viewer.id)

        with pytest.raises(BizError) as like_err:
            await like_item(db, item_id, viewer.id)
        assert like_err.value.errcode == CommonErr.FORBIDDEN

        with pytest.raises(BizError) as comment_err:
            await create_comment(
                db, item_id, viewer.id, ContentCommentCreate(content="评论")
            )
        assert comment_err.value.errcode == CommonErr.FORBIDDEN

    async def test_block_denies_comment_like(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        author = await _au(auth_db, "c_author")
        viewer = await _au(auth_db, "c_viewer")
        bid = await _board(db, "bm-comment")
        item_id = await _item(db, bid, author.id, "评论目标")
        comment = ContentComment(
            content_id=item_id, user_id=author.id, content="楼", floor_number=1
        )
        db.add(comment)
        await db.flush()
        await block_user(db, viewer.id, author.id)

        with pytest.raises(BizError) as err:
            await like_comment(db, item_id, comment.id, viewer.id)
        assert err.value.errcode == CommonErr.FORBIDDEN

    async def test_mute_does_not_restrict_interactions(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        author = await _au(auth_db, "mute_inter_author")
        viewer = await _au(auth_db, "mute_inter_viewer")
        bid = await _board(db, "bm-mute-inter")
        item_id = await _item(db, bid, author.id, "静音不禁互动")
        await mute_user(db, viewer.id, author.id)

        assert await like_item(db, item_id, viewer.id) == 1
        info = await create_comment(
            db, item_id, viewer.id, ContentCommentCreate(content="仍可评论")
        )
        assert info.author_id == viewer.id


class TestRelationshipApi:
    async def test_block_mute_endpoints(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        client: AsyncClient,
    ) -> None:
        me = await _au(auth_db, "api_me")
        other = await _au(auth_db, "api_other")
        headers = _auth_header(me)

        r = await client.post(f"/api/v1/users/{other.id}/block", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["data"] == {"blocked": True}

        r = await client.get(f"/api/v1/users/{other.id}/relationship", headers=headers)
        assert r.json()["data"] == {
            "blocked": True,
            "blocked_by": False,
            "muted": False,
        }

        r = await client.get("/api/v1/users/me/blocked", headers=headers)
        assert [i["user_id"] for i in r.json()["data"]["items"]] == [str(other.id)]

        # 自 block / 不存在目标
        self_resp = await client.post(
            f"/api/v1/users/{me.id}/block", headers=headers
        )
        assert self_resp.status_code == 400
        missing = await client.post(
            f"/api/v1/users/{uuid.uuid4()}/block", headers=headers
        )
        assert missing.status_code == 404

        r = await client.delete(f"/api/v1/users/{other.id}/block", headers=headers)
        assert r.json()["data"] == {"blocked": False}

        r = await client.post(f"/api/v1/users/{other.id}/mute", headers=headers)
        assert r.json()["data"] == {"muted": True}
        r = await client.get("/api/v1/users/me/muted", headers=headers)
        assert [i["user_id"] for i in r.json()["data"]["items"]] == [str(other.id)]
        r = await client.delete(f"/api/v1/users/{other.id}/mute", headers=headers)
        assert r.json()["data"] == {"muted": False}

    async def test_relationship_endpoints_require_login(
        self, client: AsyncClient
    ) -> None:
        other = uuid.uuid4()
        # 缺 Authorization：authz 端口按既有口径回 403（与其它登录端点一致）
        assert (
            await client.post(f"/api/v1/users/{other}/block")
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/users/{other}/relationship")
        ).status_code == 403
