"""专栏（columns）域仓储：专栏、申请与连载。

步骤 9 切片 3 自 ``content.repository`` 迁出，SQL 逐字保留。
"""

from __future__ import annotations

import uuid

from app.modules.columns.models import Column, ColumnApplication, ColumnPost
from core.db.repository import AsyncRepository


class ColumnRepository(AsyncRepository[Column]):
    model = Column

    async def get_by_slug(self, slug: str) -> Column | None:
        return await self.get_one(Column.slug == slug)

    async def get_by_application(self, application_id: uuid.UUID) -> Column | None:
        return await self.get_one(Column.application_id == application_id)

    async def list_titles(self, column_ids: set[uuid.UUID]) -> list[Column]:
        if not column_ids:
            return []
        return await self.get_many(Column.id.in_(column_ids))

    async def list_page(
        self,
        *,
        offset: int | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> list[Column]:
        """``status`` 给定时只取该状态（公开读口把可见性下推到 SQL，避免分页后再过滤）。"""
        conditions = [Column.status == status] if status is not None else []
        return await self.get_many(
            *conditions, order_by=Column.id.desc(), offset=offset, limit=limit
        )


class ColumnApplicationRepository(AsyncRepository[ColumnApplication]):
    model = ColumnApplication

    async def list_page(
        self, *, offset: int | None = None, limit: int | None = None
    ) -> list[ColumnApplication]:
        return await self.get_many(
            order_by=ColumnApplication.id.desc(), offset=offset, limit=limit
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[ColumnApplication]:
        """申请人视角：本人全部申请，最新在前（id 为 uuid7，单调递增近似时间序）。"""
        return await self.get_many(
            ColumnApplication.user_id == user_id,
            order_by=ColumnApplication.id.desc(),
        )

    async def has_pending(self, user_id: uuid.UUID) -> bool:
        return await self.exists(
            ColumnApplication.user_id == user_id,
            ColumnApplication.status == "pending",
        )

    async def latest_rejected(self, user_id: uuid.UUID) -> ColumnApplication | None:
        """该用户最近一次被拒的申请（冷却期按它的 reviewed_at 算）。"""
        return await self.get_one(
            ColumnApplication.user_id == user_id,
            ColumnApplication.status == "rejected",
            order_by=ColumnApplication.reviewed_at.desc(),
        )


class ColumnPostRepository(AsyncRepository[ColumnPost]):
    model = ColumnPost

    async def list_in_column(
        self,
        column_id: uuid.UUID,
        *,
        offset: int | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> list[ColumnPost]:
        """``status`` 给定时只取该状态（公开读口下推可见性用）。"""
        conditions: list[object] = [ColumnPost.column_id == column_id]
        if status is not None:
            conditions.append(ColumnPost.status == status)
        return await self.get_many(
            *conditions,
            order_by=ColumnPost.id.desc(),
            offset=offset,
            limit=limit,
        )
