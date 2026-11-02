"""interaction 域的仓储子类：收藏明细 + 浏览记录 + 关注关系（软删墓碑）+ 内容/板块只读缝。

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

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.modules.interaction.models import (
    BoardFollow,
    InteractionFavorite,
    InteractionViewLog,
    UserFollow,
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


class BoardFollowRepository(AsyncRepository[BoardFollow]):
    model = BoardFollow

    async def get_pair(
        self, follower_id: uuid.UUID, board_id: uuid.UUID
    ) -> BoardFollow | None:
        """取 (follower, board) 配对行——**含已软删**。"""
        return await self.get_one(
            BoardFollow.follower_id == follower_id,
            BoardFollow.board_id == board_id,
            include_deleted=True,
        )

    async def follow(self, follower_id: uuid.UUID, board_id: uuid.UUID) -> bool:
        """关注版块（幂等）：返回是否属于「新关注/复活」。"""
        row = await self.get_pair(follower_id, board_id)
        created = row is None or row.deleted_at is not None
        if row is None:
            await self.create(follower_id=follower_id, board_id=board_id)
        elif row.deleted_at is not None:
            await self.update(row, deleted_at=None)
        else:
            await self.flush()
        return created

    async def unfollow(self, follower_id: uuid.UUID, board_id: uuid.UUID) -> bool:
        """取关版块（幂等）：仅命中活动行才置墓碑。"""
        row = await self.get_pair(follower_id, board_id)
        if row is None or row.deleted_at is not None:
            return False
        await self.update(row, deleted_at=now_iso())
        return True

    async def list_board_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """我关注的所有版块 id（活动行）。"""
        rows = await self.db.execute(
            select(BoardFollow.board_id).where(
                BoardFollow.follower_id == user_id,
                BoardFollow.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())

    async def list_follower_ids(
        self, board_id: uuid.UUID, *, limit: int | None = None
    ) -> list[uuid.UUID]:
        """关注了该版块的 follower id（活动行）；``limit`` 语义同用户关注版。"""
        stmt = select(BoardFollow.follower_id).where(
            BoardFollow.board_id == board_id,
            BoardFollow.deleted_at.is_(None),
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())


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
