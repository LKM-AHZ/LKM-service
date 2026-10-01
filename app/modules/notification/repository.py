"""notification 域的仓储子类：站内信正本 + 偏好/推送 token 的 upsert。

偏好与 token 的幂等写入走基类 :meth:`AsyncRepository.pg_upsert`（PostgreSQL
``INSERT ... ON CONFLICT``），替代原先散在 service 里的 ``pg_insert`` 语句；
目标冲突键分别为 ``notification_preferences`` 的复合主键 ``(user_id, type)`` 与
``notification_tokens`` 的命名唯一约束 ``uq_notification_token``。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select, text

from app.modules.notification.models import (
    Notification,
    NotificationPreference,
    NotificationToken,
)
from core.db.repository import AsyncRepository


class NotificationRepository(AsyncRepository[Notification]):
    model = Notification

    async def lock_aggregate_key(
        self,
        *,
        user_id: uuid.UUID,
        type: str,
        actor_id: uuid.UUID,
        target_id: uuid.UUID,
    ) -> None:
        """在当前事务内串行化同一聚合键的写入。"""
        key = f"notification:{user_id}:{type}:{actor_id}:{target_id}"
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )

    async def find_recent_unread(
        self,
        *,
        user_id: uuid.UUID,
        type: str,
        actor_id: uuid.UUID,
        target_id: uuid.UUID,
        since: datetime.datetime,
    ) -> Notification | None:
        """锁住窗口内最新未读行，避免与标记已读并发时继续聚合。"""
        stmt = (
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.type == type,
                Notification.actor_id == actor_id,
                Notification.target_id == target_id,
                Notification.read_at.is_(None),
                Notification.created_at >= since,
            )
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(1)
            .with_for_update()
        )
        return (await self.db.execute(stmt)).scalars().first()


class NotificationPreferenceRepository(AsyncRepository[NotificationPreference]):
    # 复合主键 (user_id, type)：不使用按主键取行的基类方法。
    model = NotificationPreference

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        type: str,
        enabled: bool,
        now: datetime.datetime,
    ) -> None:
        """按 (user_id, type) upsert：冲突时写回 enabled 与 updated_at。"""
        await self.pg_upsert(
            {
                "user_id": user_id,
                "type": type,
                "enabled": enabled,
                "updated_at": now,
            },
            index_elements=["user_id", "type"],
            update_values={"enabled": enabled, "updated_at": now},
        )


class NotificationTokenRepository(AsyncRepository[NotificationToken]):
    model = NotificationToken

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        token: str,
        platform: str,
        now: datetime.datetime,
    ) -> None:
        """按 ``uq_notification_token`` upsert：冲突时只刷新 platform。"""
        await self.pg_upsert(
            {
                "user_id": user_id,
                "token": token,
                "platform": platform,
                "created_at": now,
            },
            constraint="uq_notification_token",
            update_values={"platform": platform},
        )
