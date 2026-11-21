"""interaction 域仓储：收藏 + 浏览记录 + 关注/订阅/watch（软删墓碑）+ 内容/板块只读缝。

- 收藏「明细行 + 计数」的计数走原子 ``UPDATE ... RETURNING greatest(count+delta,0)``，
  行不存在返回 ``None``（由 service 转 404）。
- 收藏明细的并发幂等改用 ``INSERT ... ON CONFLICT DO NOTHING RETURNING``（见
  :meth:`InteractionFavoriteRepository.add_if_absent`）：是否真的插入由返回行判定，
  无需 service 层 savepoint/捕获 ``IntegrityError``。
- 浏览记录 upsert 走基类 ``pg_upsert``（约束 ``uq_interaction_view_user_content``）。
- 关注关系的「软删墓碑」语义：follow 时已有行 ``deleted_at`` 置 NULL（复活），否则新插；
  unfollow 只置 ``deleted_at``。故取配对行必须 ``include_deleted=True``，否则复活路径
  永远看不到墓碑行。
- 跨模块只读内容/板块表（``ContentItem`` / ``Board`` / 点赞明细 ``ContentLike``）的 import
  缝由 ``interaction.repository -> content.models`` 承接。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.modules.interaction.models import (
    ContentSubscription,
    FriendRelationship,
    InteractionFavorite,
    InteractionViewLog,
    PostWatch,
    UserFollow,
    UserRelationship,
)
from core.db.base import now_iso
from core.db.repository import AsyncRepository, DbSession
from core.ports import content as content_port


class InteractionContentItemRepository:
    """interaction 侧对 ``content_items`` 的只读判定 + 收藏计数原子增减。

    实现收归 content 域（``app/modules/content/read_api.py``）：计数列、软删口径与原子
    UPDATE 都由属主维护，interaction 只经 ``core.ports.content`` 调用。
    """

    def __init__(self, db: DbSession) -> None:
        self.db = db

    async def get_bookmark_count(self, content_id: uuid.UUID) -> int | None:
        """当前收藏计数；内容行不存在返回 ``None``。"""
        return await content_port.bookmark_count(self.db, content_id)

    async def bump_bookmark_count(
        self, content_id: uuid.UUID, delta: int
    ) -> int | None:
        """原子增减 ``bookmark_count`` 并返回即时读数（下限 0）；行不存在返回 ``None``。"""
        return await content_port.bump_bookmark_count(self.db, content_id, delta)

    async def exists_content(self, content_id: uuid.UUID) -> bool:
        """内容行是否存在（浏览上报的 404 前置判定）。"""
        return await content_port.exists_content(self.db, content_id)

    async def get_viewer_state(
        self, *, content_id: uuid.UUID, user_id: uuid.UUID | None
    ) -> tuple[bool, bool, int, int] | None:
        """互动态 ``(liked, favorited, like_count, bookmark_count)``。

        内容行不存在（含已软删）返回 ``None``，由 service 转 404。``user_id`` 为 ``None``
        （匿名访客）时只回计数，``liked``/``favorited`` 恒 false，且不查明细表。
        """
        counts = await content_port.item_counts(self.db, content_id)
        if counts is None:
            return None
        like_count, bookmark_count = counts
        if user_id is None:
            return False, False, like_count, bookmark_count
        liked = await content_port.liked_by(self.db, content_id, user_id)
        favorited = await InteractionFavoriteRepository(self.db).is_favorited(
            user_id=user_id, content_id=content_id
        )
        return liked, favorited, like_count, bookmark_count


class InteractionFavoriteRepository(AsyncRepository[InteractionFavorite]):
    # 复合主键 (content_id, user_id)：按主键取行不适用，exists 需显式指定 pk_attr。
    model = InteractionFavorite
    pk_attr = "content_id"

    async def is_favorited(self, *, user_id: uuid.UUID, content_id: uuid.UUID) -> bool:
        return await self.exists(
            InteractionFavorite.user_id == user_id,
            InteractionFavorite.content_id == content_id,
        )

    async def add_if_absent(self, *, user_id: uuid.UUID, content_id: uuid.UUID) -> bool:
        """幂等落收藏明细：返回 ``True`` 表示本次真的插入了新行。

        并发撞复合主键时 ``ON CONFLICT DO NOTHING`` 不产生异常（无需 savepoint），
        返回行缺失即视为「已收藏」。
        """
        stmt = (
            pg_insert(InteractionFavorite)
            .values(content_id=content_id, user_id=user_id)
            .on_conflict_do_nothing(index_elements=["content_id", "user_id"])
            .returning(InteractionFavorite.content_id)
        )
        inserted = (await self.db.scalar(stmt)) is not None
        await self.flush()
        return inserted

    async def remove(self, *, user_id: uuid.UUID, content_id: uuid.UUID) -> int:
        """删收藏明细，返回受影响行数。"""
        return await self.hard_delete_where(
            InteractionFavorite.user_id == user_id,
            InteractionFavorite.content_id == content_id,
        )

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        """收藏总数——与 :meth:`list_page` 同口径：排除内容已软删的行（批 4），

        否则 total 与列表互相矛盾（列表 join 过滤后为空、total 仍计数）。
        """
        brief = content_port.content_brief_view()
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(InteractionFavorite)
                .join(brief, brief.c.id == InteractionFavorite.content_id)
                .where(InteractionFavorite.user_id == user_id)
            )
            or 0
        )

    async def list_page(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> list[Any]:
        """「我的收藏」分页（join 内容摘要），按 created_at/content_id 倒序。"""
        brief = content_port.content_brief_view()
        stmt = (
            select(
                InteractionFavorite.content_id,
                InteractionFavorite.created_at,
                brief.c.content_type,
                brief.c.title,
                brief.c.slug,
                brief.c.board_id,
            )
            .join(brief, brief.c.id == InteractionFavorite.content_id)
            .where(InteractionFavorite.user_id == user_id)
            .order_by(
                InteractionFavorite.created_at.desc(),
                InteractionFavorite.content_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).all())


class InteractionViewLogRepository(AsyncRepository[InteractionViewLog]):
    model = InteractionViewLog

    async def upsert_view(
        self,
        *,
        user_id: uuid.UUID,
        content_id: uuid.UUID,
        viewed_at: datetime.datetime,
    ) -> None:
        """浏览上报幂等：同 (user_id, content_id) 只刷新 ``viewed_at``。"""
        await self.pg_upsert(
            {"user_id": user_id, "content_id": content_id, "viewed_at": viewed_at},
            constraint="uq_interaction_view_user_content",
            update_columns=["viewed_at"],
        )

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        """历史总数——与 :meth:`list_page` 同口径：排除内容已软删的行（批 4）。"""
        brief = content_port.content_brief_view()
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(InteractionViewLog)
                .join(brief, brief.c.id == InteractionViewLog.content_id)
                .where(InteractionViewLog.user_id == user_id)
            )
            or 0
        )

    async def list_page(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> list[Any]:
        """「我的浏览历史」分页（join 内容摘要），按 viewed_at/id 倒序。"""
        brief = content_port.content_brief_view()
        stmt = (
            select(
                InteractionViewLog.content_id,
                InteractionViewLog.viewed_at,
                brief.c.content_type,
                brief.c.title,
                brief.c.slug,
                brief.c.board_id,
            )
            .join(brief, brief.c.id == InteractionViewLog.content_id)
            .where(InteractionViewLog.user_id == user_id)
            .order_by(InteractionViewLog.viewed_at.desc(), InteractionViewLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).all())

    async def purge_before(self, cutoff: datetime.datetime) -> int:
        """保留策略：删除 ``viewed_at < cutoff`` 的记录，返回删除行数。"""
        return await self.hard_delete_where(InteractionViewLog.viewed_at < cutoff)


class UserFollowRepository(AsyncRepository[UserFollow]):
    model = UserFollow

    async def get_pair(
        self, follower_id: uuid.UUID, following_id: uuid.UUID
    ) -> UserFollow | None:
        """取 (follower, following) 配对行——**含已软删**（复活/promotion 判定需要）。"""
        return await self.get_one(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
            include_deleted=True,
        )

    async def follow(self, follower_id: uuid.UUID, following_id: uuid.UUID) -> bool:
        """关注（幂等）：返回是否属于「新关注/复活」（供 fanout 回填判定）。

        已存在且未软删时是 no-op（仍 flush 保持原调用节奏）。
        """
        row = await self.get_pair(follower_id, following_id)
        created = row is None or row.deleted_at is not None
        if row is None:
            await self.create(follower_id=follower_id, following_id=following_id)
        elif row.deleted_at is not None:
            await self.update(row, deleted_at=None)
        else:
            await self.flush()
        return created

    async def unfollow(self, follower_id: uuid.UUID, following_id: uuid.UUID) -> bool:
        """取关（幂等）：仅命中活动行才置墓碑，返回是否真的发生变更。"""
        row = await self.get_pair(follower_id, following_id)
        if row is None or row.deleted_at is not None:
            return False
        await self.update(row, deleted_at=now_iso())
        return True

    async def list_following_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """我关注的所有用户 id（活动行，无排序——与原实现一致）。"""
        rows = await self.db.execute(
            select(UserFollow.following_id).where(
                UserFollow.follower_id == user_id,
                UserFollow.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())

    async def list_follower_ids(
        self, following_id: uuid.UUID, *, limit: int | None = None
    ) -> list[uuid.UUID]:
        """关注了该用户的 follower id（活动行）。

        ``limit`` 供 fanout 做「是否超过封顶」的早停判定（取 cap+1 即可下结论），
        为空则取全量。
        """
        stmt = select(UserFollow.follower_id).where(
            UserFollow.following_id == following_id,
            UserFollow.deleted_at.is_(None),
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_bigv_author_ids(self, threshold: int) -> list[uuid.UUID]:
        """关注者数 **超过** ``threshold`` 的作者 id（fanout 大 V 判定，读时现算）。

        判据与 fanout 的入选线一致（``len(follower_ids) > cap``，均只计未软删的关注行）。
        原先大 V 是维护在 Redis SET 里的标记，关掉 Redis 持久化后该集合重启即空且**不会自动
        重建**，已标记作者的内容会从时间线消失 —— 判据本身可从本表重放，故改为读时现算。
        """
        stmt = (
            select(UserFollow.following_id)
            .where(UserFollow.deleted_at.is_(None))
            .group_by(UserFollow.following_id)
            .having(func.count() > threshold)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def is_following(
        self, follower_id: uuid.UUID, following_id: uuid.UUID
    ) -> bool:
        """当前是否存在活动关注（基类 exists 自动施加 deleted_at 过滤）。"""
        return await self.exists(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
        )


class UserRelationshipRepository(AsyncRepository[UserRelationship]):
    """block / mute 关系读写（一行一个方向；block 双向语义由查询侧合并）。"""

    model = UserRelationship

    async def add_relation(
        self,
        user_id: uuid.UUID,
        target_id: uuid.UUID,
        kind: str,
        *,
        expires_at: datetime.datetime | None = None,
        degrade_to_status: int | None = None,
    ) -> None:
        """幂等落一行（重复时刷新过期/降级字段，空值即恢复永久）。"""
        await self.pg_upsert(
            {
                "user_id": user_id,
                "target_id": target_id,
                "kind": kind,
                "expires_at": expires_at,
                "degrade_to_status": degrade_to_status,
            },
            constraint="uq_user_relationships_kind",
            update_columns=["expires_at", "degrade_to_status"],
        )

    async def get_relation(
        self, user_id: uuid.UUID, target_id: uuid.UUID, kind: str
    ) -> UserRelationship | None:
        """该方向的单行（含过期字段）。"""
        return await self.get_one(
            UserRelationship.user_id == user_id,
            UserRelationship.target_id == target_id,
            UserRelationship.kind == kind,
        )

    async def list_relations(
        self, user_id: uuid.UUID, kind: str
    ) -> list[UserRelationship]:
        """``user_id`` 主动建立的单向关系行。"""
        return await self.get_many(
            UserRelationship.user_id == user_id,
            UserRelationship.kind == kind,
        )

    async def remove(self, user_id: uuid.UUID, target_id: uuid.UUID, kind: str) -> int:
        """删除该方向的关系行（幂等），返回受影响行数。"""
        return await self.hard_delete_where(
            UserRelationship.user_id == user_id,
            UserRelationship.target_id == target_id,
            UserRelationship.kind == kind,
        )

    async def relation_exists(
        self, user_id: uuid.UUID, target_id: uuid.UUID, kind: str
    ) -> bool:
        return await self.get_one(
            UserRelationship.user_id == user_id,
            UserRelationship.target_id == target_id,
            UserRelationship.kind == kind,
        ) is not None

    async def list_target_ids(self, user_id: uuid.UUID, kind: str) -> list[uuid.UUID]:
        """``user_id`` 主动建立的单向目标 id（如「我屏蔽/我静音了谁」）。"""
        rows = await self.db.execute(
            select(UserRelationship.target_id).where(
                UserRelationship.user_id == user_id,
                UserRelationship.kind == kind,
            )
        )
        return list(rows.scalars().all())

    async def list_blocked_either_direction(
        self, user_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """与 ``user_id`` 存在 block（任一方向）的对方 id（去重）。"""
        out = await self.db.execute(
            select(UserRelationship.target_id)
            .where(
                UserRelationship.user_id == user_id,
                UserRelationship.kind == "block",
            )
            .union(
                select(UserRelationship.user_id).where(
                    UserRelationship.target_id == user_id,
                    UserRelationship.kind == "block",
                )
            )
        )
        return list(out.scalars().all())

    async def list_block_rows_touching(
        self, user_id: uuid.UUID
    ) -> list[UserRelationship]:
        """与 ``user_id`` 相关的 block 行（任一方向，含过期信息）。"""
        return await self.get_many(
            UserRelationship.kind == "block",
            or_(
                UserRelationship.user_id == user_id,
                UserRelationship.target_id == user_id,
            ),
        )

    async def is_blocked_either_direction(
        self, user_id: uuid.UUID, other_id: uuid.UUID
    ) -> bool:
        row = await self.get_one(
            UserRelationship.kind == "block",
            or_(
                (UserRelationship.user_id == user_id)
                & (UserRelationship.target_id == other_id),
                (UserRelationship.user_id == other_id)
                & (UserRelationship.target_id == user_id),
            ),
        )
        return row is not None


class FriendRelationshipRepository(AsyncRepository[FriendRelationship]):
    """好友关系/请求（一行一个发起方向）。"""

    model = FriendRelationship

    async def get_pair(
        self, account_id: uuid.UUID, related_id: uuid.UUID
    ) -> FriendRelationship | None:
        """指定方向的行（发起人 → 目标）。"""
        return await self.get_one(
            FriendRelationship.account_id == account_id,
            FriendRelationship.related_id == related_id,
        )

    async def get_between(
        self, user_id: uuid.UUID, other_id: uuid.UUID
    ) -> FriendRelationship | None:
        """两用户之间的好友行（不区分发起方向；不存在返回 None）。"""
        return await self.get_one(
            or_(
                (FriendRelationship.account_id == user_id)
                & (FriendRelationship.related_id == other_id),
                (FriendRelationship.account_id == other_id)
                & (FriendRelationship.related_id == user_id),
            )
        )

    async def list_for_user(
        self, user_id: uuid.UUID, *, statuses: tuple[int, ...] | None = None
    ) -> list[FriendRelationship]:
        """与 ``user_id`` 相关的好友行（任一方向），可按状态过滤。"""
        stmt = select(FriendRelationship).where(
            or_(
                FriendRelationship.account_id == user_id,
                FriendRelationship.related_id == user_id,
            )
        )
        if statuses is not None:
            stmt = stmt.where(FriendRelationship.status.in_(statuses))
        rows = await self.db.execute(stmt)
        return list(rows.scalars().all())

    async def list_pending(
        self, user_id: uuid.UUID, *, now: datetime.datetime | None = None
    ) -> list[FriendRelationship]:
        """与 ``user_id`` 相关的未过期待接受请求（收件 + 已发出）。"""
        now = now or now_iso()
        stmt = select(FriendRelationship).where(
            FriendRelationship.status == 0,
            or_(
                FriendRelationship.account_id == user_id,
                FriendRelationship.related_id == user_id,
            ),
            or_(
                FriendRelationship.expires_at.is_(None),
                FriendRelationship.expires_at > now,
            ),
        )
        rows = await self.db.execute(stmt)
        return list(rows.scalars().all())

    async def delete_between(self, user_id: uuid.UUID, other_id: uuid.UUID) -> int:
        """删除两用户之间的好友行（任一方向）。"""
        return await self.hard_delete_where(
            or_(
                (FriendRelationship.account_id == user_id)
                & (FriendRelationship.related_id == other_id),
                (FriendRelationship.account_id == other_id)
                & (FriendRelationship.related_id == user_id),
            )
        )

    async def delete_pending(
        self, account_id: uuid.UUID, related_id: uuid.UUID
    ) -> int:
        """删除指定方向的待接受请求（幂等）。"""
        return await self.hard_delete_where(
            FriendRelationship.account_id == account_id,
            FriendRelationship.related_id == related_id,
            FriendRelationship.status == 0,
        )


TARGET_TYPES: tuple[str, ...] = ("board", "tag", "column")
NOTIFY_LEVELS: tuple[str, ...] = ("all", "none")


def normalize_target(target_type: str, target_id: str) -> tuple[str, str]:
    """订阅目标归一：tag 做 trim + 小写；board/column 保留原样（UUID 字符串）。"""
    if target_type == "tag":
        return target_type, target_id.strip().lower()
    return target_type, target_id


class ContentSubscriptionRepository(AsyncRepository[ContentSubscription]):
    model = ContentSubscription

    async def get_pair(
        self, user_id: uuid.UUID, target_type: str, target_id: str
    ) -> ContentSubscription | None:
        """取订阅配对行——**含已软删**（复活判定需要）。"""
        return await self.get_one(
            ContentSubscription.user_id == user_id,
            ContentSubscription.target_type == target_type,
            ContentSubscription.target_id == target_id,
            include_deleted=True,
        )

    async def subscribe(
        self, user_id: uuid.UUID, target_type: str, target_id: str, notify_level: str
    ) -> bool:
        """订阅（幂等）：返回是否属于「新订阅/复活」。活动行则只更新提醒偏好。"""
        row = await self.get_pair(user_id, target_type, target_id)
        if row is None:
            await self.create(
                user_id=user_id,
                target_type=target_type,
                target_id=target_id,
                notify_level=notify_level,
            )
            return True
        created = row.deleted_at is not None
        if created:
            await self.update(row, deleted_at=None, notify_level=notify_level)
        else:
            await self.update(row, notify_level=notify_level)
        return created

    async def unsubscribe(
        self, user_id: uuid.UUID, target_type: str, target_id: str
    ) -> bool:
        """取消订阅（幂等）：仅命中活动行才置墓碑。"""
        row = await self.get_pair(user_id, target_type, target_id)
        if row is None or row.deleted_at is not None:
            return False
        await self.update(row, deleted_at=now_iso())
        return True

    async def list_active(self, user_id: uuid.UUID) -> list[ContentSubscription]:
        """我的全部活动订阅（新订阅在前）。"""
        return await self.get_many(
            ContentSubscription.user_id == user_id,
            order_by=(
                ContentSubscription.created_at.desc(),
                ContentSubscription.id.desc(),
            ),
        )

    async def list_target_ids(
        self, user_id: uuid.UUID, target_type: str
    ) -> list[str]:
        """某类型的活动订阅目标 id（字符串形态）。"""
        rows = await self.db.execute(
            select(ContentSubscription.target_id).where(
                ContentSubscription.user_id == user_id,
                ContentSubscription.target_type == target_type,
                ContentSubscription.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())

    async def list_user_ids(
        self,
        targets: list[tuple[str, str]],
        *,
        limit: int | None = None,
        require_notify: bool = False,
    ) -> list[uuid.UUID]:
        """命中任一目标的订阅者 id（软删行排除）。

        ``require_notify=True`` 时再排除 ``notify_level='none'``——通知 fanout 用它；
        feed 受众面（订阅即关注）不传，避免「关掉提醒」顺带把内容从时间线剔除。
        ``targets`` 的 tag 目标须已归一（调用方经 :func:`normalize_target`）。
        """
        if not targets:
            return []
        pairs = [
            (ContentSubscription.target_type == target_type)
            & (ContentSubscription.target_id == target_id)
            for target_type, target_id in targets
        ]
        stmt = select(ContentSubscription.user_id).where(
            or_(*pairs),
            ContentSubscription.deleted_at.is_(None),
        )
        if require_notify:
            stmt = stmt.where(ContentSubscription.notify_level == "all")
        stmt = stmt.distinct()
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())


class PostWatchRepository(AsyncRepository[PostWatch]):
    model = PostWatch

    async def get_pair(
        self, user_id: uuid.UUID, content_id: uuid.UUID
    ) -> PostWatch | None:
        """取 watch 配对行——**含已软删**。"""
        return await self.get_one(
            PostWatch.user_id == user_id,
            PostWatch.content_id == content_id,
            include_deleted=True,
        )

    async def watch(
        self,
        user_id: uuid.UUID,
        content_id: uuid.UUID,
        notify_level: str | None = None,
    ) -> bool:
        """watch（幂等）：返回是否「新增/复活」。

        ``notify_level=None`` 表示自动 watch（收藏/评论）：已有行的偏好保持不动；
        显式路由传入等级时才覆盖。
        """
        row = await self.get_pair(user_id, content_id)
        if row is None:
            await self.create(
                user_id=user_id,
                content_id=content_id,
                notify_level=notify_level or "all",
            )
            return True
        created = row.deleted_at is not None
        values: dict[str, Any] = {}
        if created:
            values["deleted_at"] = None
        if notify_level is not None and row.notify_level != notify_level:
            values["notify_level"] = notify_level
        if values:
            await self.update(row, **values)
        else:
            await self.flush()
        return created

    async def unwatch(self, user_id: uuid.UUID, content_id: uuid.UUID) -> bool:
        """取消 watch（幂等）：仅活动行才置墓碑。"""
        row = await self.get_pair(user_id, content_id)
        if row is None or row.deleted_at is not None:
            return False
        await self.update(row, deleted_at=now_iso())
        return True

    async def list_watch_user_ids(
        self, content_id: uuid.UUID, *, limit: int | None = None
    ) -> list[uuid.UUID]:
        """该内容的 watcher id（``notify_level='all'`` 的活动行）。"""
        stmt = select(PostWatch.user_id).where(
            PostWatch.content_id == content_id,
            PostWatch.deleted_at.is_(None),
            PostWatch.notify_level == "all",
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        """watch 总数——与 :meth:`list_page` 同口径（排除内容已软删的行）。"""
        brief = content_port.content_brief_view()
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(PostWatch)
                .join(brief, brief.c.id == PostWatch.content_id)
                .where(
                    PostWatch.user_id == user_id,
                    PostWatch.deleted_at.is_(None),
                )
            )
            or 0
        )

    async def list_page(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> list[Any]:
        """「我的 watch」分页（join 内容摘要），按 created_at/content_id 倒序。"""
        brief = content_port.content_brief_view()
        stmt = (
            select(
                PostWatch.content_id,
                PostWatch.created_at,
                PostWatch.notify_level,
                brief.c.content_type,
                brief.c.title,
                brief.c.slug,
                brief.c.board_id,
            )
            .join(brief, brief.c.id == PostWatch.content_id)
            .where(
                PostWatch.user_id == user_id,
                PostWatch.deleted_at.is_(None),
            )
            .order_by(PostWatch.created_at.desc(), PostWatch.content_id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).all())


class BoardReadRepository:
    """板块表**只读**缝（关注目标存在性 + 标题回填）。

    经 content 提供的可 join 视图读取，interaction 不持有 ``Board`` 模型。
    """

    def __init__(self, db: DbSession) -> None:
        self.db = db

    async def get(self, board_id: uuid.UUID) -> Any | None:
        """按主键取板块摘要（不存在 → ``None``）。"""
        brief = content_port.board_brief_view()
        return (
            await self.db.execute(select(brief).where(brief.c.id == board_id))
        ).first()

    async def title_map(self, board_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """板块 id → 标题。"""
        if not board_ids:
            return {}
        brief = content_port.board_brief_view()
        rows = (
            await self.db.execute(
                select(brief.c.id, brief.c.title).where(brief.c.id.in_(board_ids))
            )
        ).all()
        return {row.id: row.title for row in rows}
