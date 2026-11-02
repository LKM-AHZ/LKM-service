"""问答（QA）域的时间线源查询（供 feed 读取）。

查询条件与游标语义留在属主域，feed 只做 ``FeedItem`` 映射。步骤 9 切片 1 自
``content.feed_api`` 迁出，SQL 逐字不变。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.sql import Select

from app.contracts.feed import cursor_order
from app.modules.qa.models import QAQuestion


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
    """取一窗问答源行（未执行）：仅 open/accepted 可见。"""
    conditions: list[Any] = [QAQuestion.status.in_(["open", "accepted"])]
    if author_ids is not None:
        conditions.append(QAQuestion.author_id.in_(author_ids))
    order_by = cursor_order(
        QAQuestion.created_at,
        QAQuestion.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    return select(QAQuestion).where(*conditions).order_by(*order_by).limit(limit)
