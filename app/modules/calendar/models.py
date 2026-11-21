"""日历域模型：用户事件 + 日历订阅。

``owner_id`` 是 auth 库用户的裸 UUID（S5 拆库后业务库不做物理外键）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import (
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class CalendarEvent(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """用户日程事件（软删）。重复规则存在 ``recurrence`` JSONB 里。"""

    __tablename__: str = "calendar_events"
    __table_args__: tuple[Any, ...] = (
        Index("ix_calendar_events_owner_start", "owner_id", "start_time"),
        Index("ix_calendar_events_owner_end", "owner_id", "end_time"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    start_time: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    end_time: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 0=private, 100=friends, 200=public（与客户端枚举一致）
    visibility: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recurrence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # LKM 文件库 id；当前前端渲染端不消费（沿用 Solar 文件引用形状为 null）。
    icon_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    background_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class CalendarSubscription(Base):
    """订阅另一个账号的公开日历（复合主键保证幂等）。"""

    __tablename__: str = "calendar_subscriptions"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
