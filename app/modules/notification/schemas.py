"""notification REST 出入参。"""

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    actor_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime.datetime | None = None
    created_at: datetime.datetime


class UnreadCountOut(BaseModel):
    unread: int


class MarkReadIn(BaseModel):
    """标记已读：给 ``ids`` 或 ``all=true``（二者都空则 no-op）。"""

    # 直接进 Notification.id.in_(ids)：无上限时可被塞进巨型 IN（PG 绑定参数上限 65535，
    # 超出直接报错变 500），也白耗内存与查询时间
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    all: bool = False


class MarkReadOut(BaseModel):
    updated: int


class PreferenceOut(BaseModel):
    type: str
    enabled: bool


class PreferencesOut(BaseModel):
    items: list[PreferenceOut]


class PreferenceItemIn(BaseModel):
    type: str
    enabled: bool


class PreferencesIn(BaseModel):
    items: list[PreferenceItemIn]


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=255)
    platform: str = Field(default="web", min_length=1, max_length=20)


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    token: str
    platform: str
    created_at: datetime.datetime
