"""专栏（columns）域的时间线源查询（供 feed 读取）。

查询条件与游标语义留在属主域，feed 只做 ``FeedItem`` 映射。步骤 9 切片 3 自
``content.feed_api`` 迁出，SQL 逐字不变。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.sql import Select

from app.contracts.feed import cursor_order
from app.modules.columns.column_models import ColumnPostStatus
from app.modules.columns.models import ColumnPost


def select_source(
    *,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    after_time: datetime.datetime | None,
    after_id: uuid.UUID | None,
    limit: int,
) -> Select[Any]:
    """取一窗专栏连载源行（未执行）：仅已发布可见。"""
    conditions: list[Any] = [ColumnPost.status == ColumnPostStatus.PUBLISHED]
    if author_ids is not None:
        conditions.append(ColumnPost.author_id.in_(author_ids))
    order_by = cursor_order(
        ColumnPost.created_at,
        ColumnPost.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    return select(ColumnPost).where(*conditions).order_by(*order_by).limit(limit)
