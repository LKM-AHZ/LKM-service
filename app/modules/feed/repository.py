"""feed 域的仓储子类：物化时间线读。

关注关系（``UserFollow``/``BoardFollow``）与板块只读缝已随其归属迁入
``interaction.repository``——本域不再直接触达那几张表，也不再有跨模块只读缝。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from app.db.repository import AsyncRepository
from app.modules.feed.models import FeedItemMaterialized


class FeedItemMaterializedRepository(AsyncRepository[FeedItemMaterialized]):
    model = FeedItemMaterialized

    async def list_page(
        self,
        user_id: uuid.UUID,
        *,
        before_time: datetime.datetime | None,
        before_id: uuid.UUID | None,
        limit: int,
    ) -> list[FeedItemMaterialized]:
        """物化表按 (created_at, id) 游标取一页（时间倒序）。

        下滤条件与 ``feed.feed._before_conds`` 严格同式（本类自持一份，避免仓库层
        反向依赖实时合流模块）；同理，``before_id`` 缺失时退化为纯时间下滤——
        不能保留 ``id < NULL``（SQL 中恒为 NULL，会把同一时刻的行整批漏掉）。
        """
        conds: list[Any] = [FeedItemMaterialized.user_id == user_id]
        if before_time is not None:
            if before_id is None:
                conds.append(FeedItemMaterialized.created_at < before_time)
            else:
                conds.append(
                    (FeedItemMaterialized.created_at < before_time)
                    | (
                        (FeedItemMaterialized.created_at == before_time)
                        & (FeedItemMaterialized.id < before_id)
                    )
                )
        return await self.get_many(
            *conds,
            order_by=(
                FeedItemMaterialized.created_at.desc(),
                FeedItemMaterialized.id.desc(),
            ),
            limit=limit,
        )
