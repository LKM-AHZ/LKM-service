"""interaction 域的仓储子类：收藏明细 + 浏览记录 + 内容表只读缝。

- 收藏「明细行 + 计数」的计数走原子 ``UPDATE ... RETURNING greatest(count+delta,0)``，
  行不存在返回 ``None``（由 service 转 404）。
- 收藏明细的并发幂等改用 ``INSERT ... ON CONFLICT DO NOTHING RETURNING``（见
  :meth:`InteractionFavoriteRepository.add_if_absent`）：是否真的插入由返回行判定，
  无需 service 层 savepoint/捕获 ``IntegrityError``。
- 浏览记录 upsert 走基类 ``pg_upsert``（约束 ``uq_interaction_view_user_content``）。
- 跨模块只读内容表（``ContentItem``）的 import 缝由
  ``interaction.repository -> content.models`` 承接（原豁免在
  ``interaction.service -> content.models``）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.repository import AsyncRepository
from app.modules.content.models import ContentItem
from app.modules.interaction.models import InteractionFavorite, InteractionViewLog


class InteractionContentItemRepository(AsyncRepository[ContentItem]):
    """interaction 侧对 ``content_items`` 的只读判定 + 收藏计数原子增减。"""

    model = ContentItem

    async def get_bookmark_count(self, content_id: uuid.UUID) -> int | None:
        """当前收藏计数；内容行不存在返回 ``None``。"""
        return await self.db.scalar(
            select(ContentItem.bookmark_count).where(
                ContentItem.id == content_id,
                ContentItem.deleted_at.is_(None),
            )
        )

    async def bump_bookmark_count(
        self, content_id: uuid.UUID, delta: int
    ) -> int | None:
        """原子增减 ``bookmark_count`` 并返回即时读数（下限 0）；行不存在返回 ``None``。

        「不存在」也包含已软删（``deleted_at`` 非空）——与同类的 ``get_bookmark_count`` /
        ``exists_content`` 同口径：否则写路径会对一条读路径报 404 的内容照改计数，读不到却
        改得动（service 的先读守卫是另一条语句，中间并发软删即可穿透）。
        """
        result = await self.db.execute(
            sa_update(ContentItem)
            .where(
                ContentItem.id == content_id,
                ContentItem.deleted_at.is_(None),
            )
            .values(bookmark_count=func.greatest(ContentItem.bookmark_count + delta, 0))
            .returning(ContentItem.bookmark_count)
        )
        row: Any = result.first()
        return None if row is None else int(row[0])

    async def exists_content(self, content_id: uuid.UUID) -> bool:
        """内容行是否存在（浏览上报的 404 前置判定）。"""
        return await self.exists(ContentItem.id == content_id)


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
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(InteractionFavorite)
                .join(ContentItem, ContentItem.id == InteractionFavorite.content_id)
                .where(
                    InteractionFavorite.user_id == user_id,
                    ContentItem.deleted_at.is_(None),
                )
            )
            or 0
        )

    async def list_page(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> list[Any]:
        """「我的收藏」分页（join 内容摘要），按 created_at/content_id 倒序。"""
        stmt = (
            select(
                InteractionFavorite.content_id,
                InteractionFavorite.created_at,
                ContentItem.content_type,
                ContentItem.title,
                ContentItem.slug,
                ContentItem.board_id,
            )
            .join(ContentItem, ContentItem.id == InteractionFavorite.content_id)
            .where(InteractionFavorite.user_id == user_id)
            # 内容软删（批 4）后不再出现在「我的收藏」里
            .where(ContentItem.deleted_at.is_(None))
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
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(InteractionViewLog)
                .join(ContentItem, ContentItem.id == InteractionViewLog.content_id)
                .where(
                    InteractionViewLog.user_id == user_id,
                    ContentItem.deleted_at.is_(None),
                )
            )
            or 0
        )

    async def list_page(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> list[Any]:
        """「我的浏览历史」分页（join 内容摘要），按 viewed_at/id 倒序。"""
        stmt = (
            select(
                InteractionViewLog.content_id,
                InteractionViewLog.viewed_at,
                ContentItem.content_type,
                ContentItem.title,
                ContentItem.slug,
                ContentItem.board_id,
            )
            .join(ContentItem, ContentItem.id == InteractionViewLog.content_id)
            .where(InteractionViewLog.user_id == user_id)
            # 内容软删（批 4）后不再出现在「我的浏览历史」里
            .where(ContentItem.deleted_at.is_(None))
            .order_by(InteractionViewLog.viewed_at.desc(), InteractionViewLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).all())

    async def purge_before(self, cutoff: datetime.datetime) -> int:
        """保留策略：删除 ``viewed_at < cutoff`` 的记录，返回删除行数。"""
        return await self.hard_delete_where(InteractionViewLog.viewed_at < cutoff)
