"""时间线游标契约：``(created_at, id)`` 键集分页的条件与排序。

放在 contracts 是因为**每个内容源都要用同一套游标语义**（discussion/article/column/qa/
project/blog），而条件构造只依赖列对象、不依赖任何域。由各域在自建查询里调用，避免
同一段边界逻辑在各处复制后漂移。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any


def before_conditions(
    col_time: Any,
    model_id: Any,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
) -> list[Any]:
    """(created_at, id) 水位下滤条件（读路径降序分页）。"""
    if before_time is None:
        return []
    if before_id is None:
        return [col_time < before_time]
    return [
        (col_time < before_time) | ((col_time == before_time) & (model_id < before_id))
    ]


def after_conditions(
    col_time: Any,
    model_id: Any,
    after_time: datetime.datetime,
    after_id: uuid.UUID | None,
) -> list[Any]:
    """(created_at, id) 水位上滤条件（fanout 升序增量）。"""
    if after_id is None:
        return [col_time > after_time]
    return [
        (col_time > after_time) | ((col_time == after_time) & (model_id > after_id))
    ]


def cursor_order(
    col_time: Any,
    model_id: Any,
    conditions: list[Any],
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    after_time: datetime.datetime | None,
    after_id: uuid.UUID | None,
) -> tuple[Any, ...]:
    """按游标方向**就地**给 ``conditions`` 追加过滤，返回 ``order_by`` 元组。"""
    if after_time is not None:
        conditions.extend(after_conditions(col_time, model_id, after_time, after_id))
        return (col_time.asc(), model_id.asc())
    if before_time is not None:
        conditions.extend(before_conditions(col_time, model_id, before_time, before_id))
    return (col_time.desc(), model_id.desc())
