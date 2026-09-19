"""notification 域的仓储子类：站内信正本 + 偏好/推送 token 的 upsert。

偏好与 token 的幂等写入走基类 :meth:`AsyncRepository.pg_upsert`（PostgreSQL
``INSERT ... ON CONFLICT``），替代原先散在 service 里的 ``pg_insert`` 语句；
目标冲突键分别为 ``notification_preferences`` 的复合主键 ``(user_id, type)`` 与
``notification_tokens`` 的命名唯一约束 ``uq_notification_token``。
"""

from __future__ import annotations

import datetime
import uuid

from app.db.repository import AsyncRepository
from app.modules.notification.models import (
    Notification,
    NotificationPreference,
    NotificationToken,
)


class NotificationRepository(AsyncRepository[Notification]):
    model = Notification

    async def find_recent_unread(
        self,
        *,
        user_id: uuid.UUID,
        type: str,
        actor_id: uuid.UUID,
        target_id: uuid.UUID,
        since: datetime.datetime,
    ) -> Notification | None:
        """聚合窗口内同一 (actor, target) 的最近一条未读通知（id 倒序取首）。"""
        return await self.get_one(
            Notification.user_id == user_id,
            Notification.type == type,
            Notification.actor_id == actor_id,
            Notification.target_id == target_id,
            Notification.read_at.is_(None),
            Notification.created_at >= since,
            order_by=Notification.id.desc(),
        )


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
