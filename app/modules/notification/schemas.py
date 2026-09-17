"""notification REST 出入参。"""

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    actor_id: int | None = None
    target_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime.datetime | None = None
    created_at: datetime.datetime


class UnreadCountOut(BaseModel):
    unread: int


class MarkReadIn(BaseModel):
    """标记已读：给 ``ids`` 或 ``all=true``（二者都空则 no-op）。"""

    ids: list[int] = Field(default_factory=list)
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
    platform: str = Field(default="web", max_length=20)


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    token: str
    platform: str
    created_at: datetime.datetime
