"""日历事件请求体。

布尔/时间/枚举字段名与桌面端 wire 一致（snake_case）：客户端直接发 Solar 形状的
JSON，服务端无需中间适配。
"""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, Field

VISIBILITIES = (0, 100, 200)


class RecurrenceIn(BaseModel):
    frequency: int = Field(default=0, ge=0, le=4)
    interval: int = Field(default=1, ge=1, le=365)
    end_date: datetime.datetime | None = None
    occurrences: int | None = Field(default=None, ge=0, le=1000)
    days_of_week: list[str] | None = None
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    month_of_year: int | None = Field(default=None, ge=1, le=12)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    start_time: datetime.datetime
    end_time: datetime.datetime
    description: str | None = Field(default=None, max_length=4096)
    location: str | None = Field(default=None, max_length=512)
    is_all_day: bool = False
    visibility: int = Field(default=0)
    recurrence: RecurrenceIn | None = None
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] | None = None
    icon_id: str | None = Field(default=None, max_length=36)
    background_id: str | None = Field(default=None, max_length=36)


class EventUpdate(BaseModel):
    """只更新显式提供的字段。

    ``recurrence`` 显式为 ``null`` 表示清除重复规则（客户端在 frequency=none 时正是
    这么发的），因此不能把「字段缺失」与「值为 null」混为一谈——用
    ``model_fields_set`` 判定是否提供。
    """

    title: str | None = Field(default=None, min_length=1, max_length=256)
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    description: str | None = Field(default=None, max_length=4096)
    location: str | None = Field(default=None, max_length=512)
    is_all_day: bool | None = None
    visibility: int | None = None
    recurrence: RecurrenceIn | None = None
    tags: list[str] | None = None
    meta: dict[str, Any] | None = None
    icon_id: str | None = Field(default=None, max_length=36)
    background_id: str | None = Field(default=None, max_length=36)
