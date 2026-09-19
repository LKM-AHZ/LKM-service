"""notification 业务逻辑：站内信产生（含同类聚合）、已读、偏好、推送 token。

设计口径：
- **以库为准**：通知先落 ``notifications`` 正本，WS 推送是 best-effort 副本（见 tasks.py）。
- **偏好门控只作用于实时推送**：关掉某类型仍落库（站内信不丢），只是不实时推送；
  用户可在列表里看到（这是「只落库不推」一侧的选择，避免静默丢通知）。
- **同类聚合**：同一 ``(user_id, type, actor_id, target_id)`` 在聚合窗口内的未读通知合并为
  一条（``payload.count`` 累加、``created_at`` 刷新），防通知风暴。
- **类型白名单**：偏好接口只接受 ``NOTIFICATION_TYPES`` 内的类型，杜绝脏偏好行。
"""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Any

from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError
from app.db.repository import DbSession
from app.modules.notification.errors import NotificationErr
from app.modules.notification.models import (
    Notification,
    NotificationPreference,
    NotificationToken,
)
from app.modules.notification.repository import (
    NotificationPreferenceRepository,
    NotificationRepository,
    NotificationTokenRepository,
)
from app.modules.notification.schemas import (
    NotificationOut,
    PreferenceOut,
    TokenOut,
)


class NotificationType(StrEnum):
    """已支持的通知类型（偏好接口的白名单）。"""

    CONTENT_LIKED = "content_liked"
    CONTENT_COMMENTED = "content_commented"
    COMMENT_REPLIED = "comment_replied"


NOTIFICATION_TYPES: tuple[str, ...] = tuple(t.value for t in NotificationType)


async def create_notification(
    db: DbSession,
    *,
    user_id: uuid.UUID,
    type: str,
    actor_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    aggregate_window_s: float = 3600.0,
) -> Notification:
    """落一条通知（同 actor+target 的未读同类通知在窗口内合并，返回被合并的那条）。

    不 commit——与调用方事务同生共死（消费者侧由 tasks.py 提交）。
    """
    now = datetime.datetime.now(datetime.UTC)
    repo = NotificationRepository(db)
    if actor_id is not None and target_id is not None and aggregate_window_s > 0:
        existing = await repo.find_recent_unread(
            user_id=user_id,
            type=type,
            actor_id=actor_id,
            target_id=target_id,
            since=now - datetime.timedelta(seconds=aggregate_window_s),
        )
        if existing is not None:
            merged = dict(existing.payload or {})
            merged["count"] = int(merged.get("count", 1)) + 1
            # 赋新 dict：JSONB 无变更追踪，原地改不会被 SQLAlchemy 感知
            existing.payload = merged
            existing.created_at = now
            await repo.flush()
            return existing

    return await repo.add(
        Notification(
            user_id=user_id,
            type=type,
            actor_id=actor_id,
            target_id=target_id,
            payload=payload or {},
            created_at=now,
        )
    )


async def list_notifications(
    db: DbSession,
    user_id: uuid.UUID,
    page: int = 1,
    limit: int = 20,
    unread_only: bool = False,
) -> PageData[NotificationOut]:
    repo = NotificationRepository(db)
    conditions = [Notification.user_id == user_id]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))
    total = await repo.count(*conditions)
    rows = await repo.get_many(
        *conditions,
        order_by=Notification.id.desc(),
        offset=paginate_offset(page, limit),
        limit=limit,
    )
    return PageData(
        items=[NotificationOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def unread_count(db: DbSession, user_id: uuid.UUID) -> int:
    return await NotificationRepository(db).count(
        Notification.user_id == user_id, Notification.read_at.is_(None)
    )


async def mark_read(
    db: DbSession, user_id: uuid.UUID, ids: list[uuid.UUID], all_: bool = False
) -> int:
    """标记已读：``all_`` 优先；否则按 ids（只命中自己的未读行）。返回更新行数。"""
    conditions = [Notification.user_id == user_id, Notification.read_at.is_(None)]
    if not all_:
        if not ids:
            return 0
        conditions.append(Notification.id.in_(ids))
    return await NotificationRepository(db).update_where(
        {"read_at": datetime.datetime.now(datetime.UTC)}, *conditions
    )


async def list_preferences(db: DbSession, user_id: uuid.UUID) -> list[PreferenceOut]:
    """返回全部已知类型 + 其开关（无行 = 默认开）。"""
    rows = await NotificationPreferenceRepository(db).get_many(
        NotificationPreference.user_id == user_id
    )
    stored = {r.type: r.enabled for r in rows}
    return [
        PreferenceOut(type=t, enabled=stored.get(t, True)) for t in NOTIFICATION_TYPES
    ]


async def set_preferences(
    db: DbSession, user_id: uuid.UUID, items: list[tuple[str, bool]]
) -> list[PreferenceOut]:
    """局部更新偏好（白名单校验后 upsert），返回更新后的全量偏好。"""
    now = datetime.datetime.now(datetime.UTC)
    repo = NotificationPreferenceRepository(db)
    for type_, enabled in items:
        if type_ not in NOTIFICATION_TYPES:
            raise BizError(NotificationErr.INVALID_TYPE, f"未知通知类型: {type_}")
        await repo.upsert(user_id=user_id, type=type_, enabled=enabled, now=now)
    return await list_preferences(db, user_id)


async def is_type_enabled(db: DbSession, user_id: uuid.UUID, type: str) -> bool:
    """该用户该类型是否开启实时推送（无行 = 开）。"""
    row = await NotificationPreferenceRepository(db).get_one(
        NotificationPreference.user_id == user_id,
        NotificationPreference.type == type,
    )
    return True if row is None else bool(row.enabled)


async def register_token(
    db: DbSession, user_id: uuid.UUID, token: str, platform: str = "web"
) -> TokenOut:
    """注册推送 token：同 (user_id, token) 幂等（刷新 platform）。"""
    now = datetime.datetime.now(datetime.UTC)
    repo = NotificationTokenRepository(db)
    await repo.upsert(user_id=user_id, token=token, platform=platform, now=now)
    row = await repo.get_one(
        NotificationToken.user_id == user_id,
        NotificationToken.token == token,
    )
    return TokenOut.model_validate(row)


async def delete_token(db: DbSession, user_id: uuid.UUID, token: str) -> int:
    """注销推送 token（只能删自己的）。"""
    return await NotificationTokenRepository(db).hard_delete_where(
        NotificationToken.user_id == user_id, NotificationToken.token == token
    )
