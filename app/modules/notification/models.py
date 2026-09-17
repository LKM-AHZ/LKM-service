"""notification 域模型：站内信 / 偏好 / 推送 token（M6.8）。

- ``notifications``：以库为准的站内信正本（WS 推送是 best-effort 副本）。
  ``actor_id``/``target_id`` 是**聚合键**（同一人对同一目标重复触发时合并，见 service），
  ``payload`` 存前端渲染所需的展示字段（标题/摘要/跳转）。
- ``notification_preferences``：``(user_id, type)`` 复合主键；**无行 = 默认开启**，
  故新增通知类型无需回填历史用户。
- ``notification_tokens``：``(user_id, token)`` 唯一，token 换绑同用户幂等。

``user_id`` 一律裸 Integer（S5 拆库后 auth 库是用户权威，业务库不建物理外键）。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, now_iso


class Notification(Base):
    """站内信正本（一条 = 一次对某用户的可见通知）。"""

    __tablename__: str = "notifications"
    __table_args__: tuple[Any, ...] = (
        # 「我的通知」按 id 倒序翻页
        Index("ix_notifications_user_id", "user_id", "id"),
        # 未读数统计（read_at is null）
        Index("ix_notifications_user_read", "user_id", "read_at"),
        # 聚合判定：同 (user_id,type,actor,target) 的未读行
        Index(
            "ix_notifications_aggregate",
            "user_id",
            "type",
            "actor_id",
            "target_id",
            "read_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    # 触发者（auth user id）；系统通知为 None
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 目标实体 id（当前为 content_items.id）；系统通知为 None
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    read_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class NotificationPreference(Base):
    """按类型开关；无行 = 默认开启（新增类型无需回填）。"""

    __tablename__: str = "notification_preferences"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class NotificationToken(Base):
    """移动/浏览器推送 token（(user_id, token) 唯一，换绑幂等）。"""

    __tablename__: str = "notification_tokens"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("user_id", "token", name="uq_notification_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
