"""projects 域的时间线源查询（供 feed 读取）。"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.sql import Select

from app.contracts.feed import cursor_order
from app.modules.projects.models import Project


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
    """活跃项目的按游标一窗（未执行）。"""
    conditions: list[Any] = [Project.status == "active"]
    if author_ids is not None:
        conditions.append(Project.applicant_id.in_(author_ids))
    order_by = cursor_order(
        Project.created_at,
        Project.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    return select(Project).where(*conditions).order_by(*order_by).limit(limit)
