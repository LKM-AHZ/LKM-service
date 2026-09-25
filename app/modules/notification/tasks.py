"""notification 订阅任务：消费既有业务行为事件生成站内信（M6.8）。

订阅 ``notification`` 与 points 三订阅**同 topic**（``biz/points.apply``）但订阅名不同，
故各收全量事件、各记各的幂等账（``worker._dispatch_with_dedup`` 的 scope = 订阅名）。

只处理「针对某内容/某评论」且目标可解析的事件：
- ``like`` + ``item:{id}``      → 通知内容作者 ``content_liked``
- ``comment`` + ``comment:{id}`` → 通知内容作者 ``content_commented``；
  若是回复（``parent_id`` 非空）另通知父评论作者 ``comment_replied``

不产通知的事件：``post``/``competition``/``file_approved``/``article:like`` 等是
「自己触发自己」或目标实体不在统一内容表；``answer_accepted`` 的事件主体就是回答作者
本人（缺提问者信息），无法定向。

落库与推送的关系：**以库为准**——先入库并提交，再 best-effort 推送（Redis/WS 失败不影响
通知存在）；偏好关闭只静音实时推送，库里的站内信照落（用户刷新仍可见）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.messaging import SUB_NOTIFICATION
from app.core.task_registry import register_task
from app.db.session import new_worker_session as new_session
from app.modules.content.models import ContentComment, ContentItem
from app.modules.notification.service import (
    NotificationType,
    create_notification,
    is_type_enabled,
)
from app.ws.broker import publish_notification
from auth.snapshot import get_user_snapshot

logger = logging.getLogger(__name__)


class _Target(NamedTuple):
    """一条待生成通知的目标：收件人 + 类型 + 聚合目标 + 展示字段。"""

    owner_id: uuid.UUID
    kind: str
    target_id: uuid.UUID
    payload: dict[str, Any]


def _split_ref(ref_id: str) -> tuple[str, uuid.UUID] | None:
    """解析 ``{prefix}:{id}`` 形式的 ref_id；格式不符返回 None。"""
    prefix, sep, raw = ref_id.partition(":")
    if not sep:
        return None
    try:
        return prefix, uuid.UUID(raw)
    except ValueError:
        return None


async def _resolve_targets(db: AsyncSession, event: str, ref_id: str) -> list[_Target]:
    """把行为事件解析为通知目标列表（无法解析/无作者 → 空列表，静默跳过）。"""
    parsed = _split_ref(ref_id)
    if parsed is None:
        return []
    prefix, ref = parsed

    if event == "like" and prefix == "item":
        # 软删（批 4）内容不再产生新通知（事件可能晚于删除到达）
        item = await db.scalar(
            select(ContentItem).where(
                ContentItem.id == ref, ContentItem.deleted_at.is_(None)
            )
        )
        if item is None or item.author_id is None:
            return []
        return [
            _Target(
                owner_id=item.author_id,
                kind=NotificationType.CONTENT_LIKED,
                target_id=item.id,
                payload={
                    "title": item.title,
                    "url": f"/content/posts/{item.id}",
                },
            )
        ]

    if event == "comment" and prefix == "comment":
        comment = await db.scalar(
            select(ContentComment).where(
                ContentComment.id == ref, ContentComment.deleted_at.is_(None)
            )
        )
        if comment is None:
            return []
        item = await db.scalar(
            select(ContentItem).where(
                ContentItem.id == comment.content_id,
                ContentItem.deleted_at.is_(None),
            )
        )
        if item is None:
            return []
        base: dict[str, Any] = {
            "title": item.title,
            "url": f"/content/posts/{item.id}",
            "comment_id": str(comment.id),
        }
        targets: list[_Target] = []
        if item.author_id is not None:
            targets.append(
                _Target(
                    owner_id=item.author_id,
                    kind=NotificationType.CONTENT_COMMENTED,
                    target_id=item.id,
                    payload=dict(base),
                )
            )
        if comment.parent_id is not None:
            parent = await db.scalar(
                select(ContentComment).where(
                    ContentComment.id == comment.parent_id,
                    ContentComment.deleted_at.is_(None),
                )
            )
            if parent is not None:
                targets.append(
                    _Target(
                        owner_id=parent.user_id,
                        kind=NotificationType.COMMENT_REPLIED,
                        target_id=item.id,
                        payload=dict(base),
                    )
                )
        return targets

    return []


async def _actor_name(db: AsyncSession, actor_id: uuid.UUID) -> str:
    """触发者展示名（展示增强项：任何失败都降级为空串，不能让通知丢失）。

    snapshot 读缝自身对「AUTH 不可达」是 fail-open 的，但若 seam 关闭且业务库无
    ``users`` 表（拆库后的真实部署）会抛 ``UndefinedTable``——此处兜住并回滚，使
    当前事务恢复可用（调用点在所有写入之前，回滚无副作用）。
    """
    try:
        snap = await get_user_snapshot(db, user_id=actor_id)
    except Exception:
        logger.warning("actor name lookup failed uid=%s; 降级为空名", actor_id)
        await db.rollback()
        return ""
    return snap.display_name if snap is not None else ""


async def notify_from_point_event(
    actor_id: uuid.UUID | str, event: str, ref_id: str
) -> None:
    """订阅 handler：解析事件 → 落站内信 → 提交后推送（best-effort）。"""
    # 事件链路（outbox JSONB → Pulsar JSON）会把 uuid 序列化成字符串（见
    # db/outbox._jsonable 的消费侧契约），直调路径传的才是 UUID 对象。这里统一归一：
    # 否则下面 `t.owner_id == actor_id` 是 UUID==str 恒 False，自己触发自己的去重失效，
    # 且快照/入库的 Uuid 绑定拿到 str 也会走偏。
    actor_id = uuid.UUID(str(actor_id))
    db = await new_session()
    pending: list[tuple[uuid.UUID, uuid.UUID, str, dict[str, Any], bool]] = []
    try:
        targets = await _resolve_targets(db, event, ref_id)
        if targets:
            actor_name = await _actor_name(db, actor_id)
            for t in targets:
                if t.owner_id == actor_id:
                    continue  # 自己触发的不通知自己
                enabled = await is_type_enabled(db, t.owner_id, t.kind)
                row = await create_notification(
                    db,
                    user_id=t.owner_id,
                    type=t.kind,
                    actor_id=actor_id,
                    target_id=t.target_id,
                    payload={
                        **t.payload,
                        "actor_id": str(actor_id),
                        "actor_name": actor_name,
                    },
                    aggregate_window_s=settings.notification_aggregate_window_s,
                )
                # commit 后 ORM 对象会 expire，故先把推送所需字段取成普通值
                pending.append(
                    (row.user_id, row.id, row.type, dict(row.payload), enabled)
                )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

    for user_id, nid, type_, payload, enabled in pending:
        if not enabled:
            continue
        # 聚合更新复用同一行（id 不变）而 payload.count 递增：若 event_id 仍是 notification:{id}，
        # 前端按「同 event_id 重推」去重会把这次内容变化丢掉（未读数停在旧值）。故 count>1
        # （即确实发生过聚合）时把计数并进 event_id，让每次内容变化都成为一次新事件；
        # 首次推送保持原 event_id 格式，不动既有前端契约。
        count = int(payload.get("count", 1) or 1)
        event_id = f"notification:{nid}" if count <= 1 else f"notification:{nid}:{count}"
        await publish_notification(
            user_id,
            {
                "event": "notification_created",
                "notification_id": str(nid),
                "type": type_,
                "payload": payload,
            },
            event_id=event_id,
            version=nid.int,
        )


register_task(SUB_NOTIFICATION.name, "apply_point_event", notify_from_point_event)
