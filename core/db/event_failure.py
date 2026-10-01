"""
outbox 发布失败归档表（M1 gate review 收口，路线图 §4 M1.3）。
relay 有**两条**折叠路径把行从 `outbox_events` **摘除**迁入本表——都是「删原行」而**不是**
置某个终态（`outbox_events` 不会出现 `status=failed` 的行，本表也没有 status 列）：
1. **瞬时失败耗竭**：投递反复失败致 ``attempt_count`` 达 `MAX_TRIES` 后不再重投，
   ``reason="relay exhausted: max tries reached"``，此类 ``attempt_count >= MAX_TRIES``。
2. **确定性永久失败**：投递前经 ``messaging.permanent_failure_reason`` 判定为「重试无意义」
   （未知 routing_key / payload 不可 JSON 编码）→ **首次尝试即折叠**，不消耗重试额度，
   故此类行的 ``attempt_count`` 可能为 0。
**查询/过滤不得假定 ``attempt_count >= MAX_TRIES``**（第 2 类不满足此不变量）；要区分两类看
``reason``。折叠后不再挤占 relay 领取窗口/积压 gauge；人工重放标记 replayed_at，原记录留审计。
与消费侧 DMQ(`dlq_messages`) 故障域隔离：本表只管「relay 发布侧投不出」，消费侧失败仍进
Pulsar 死信 topic（system/dlq）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core import messaging
from core.config import settings
from core.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso
from core.db.outbox import OUTBOX_PENDING, OutboxMessage, enqueue_outbox


class EventFailure(UUIDPrimaryKeyMixin, Base):
    """relay 发布失败（重试耗竭 **或** 确定性永久失败）而迁出的归档事件。

    ``event_id`` 即审计锚点；两条折叠路径及其 attempt_count 取值差异见模块 docstring。
    """

    __tablename__: str = "event_failures"

    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 逻辑主题 = 将投失败时的 routing_key
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 与 outbox_events.payload_json 同构的全量 {fn,args} dict；含透传的 event_id
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    folded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    replayed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


async def replay_failure(
    db: AsyncSession,
    failure_id: uuid.UUID,
    *,
    routing_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """沿用 event_id 重新入队，保留原审计行并标记 replayed_at。

    行锁串行同一失败行的人工操作；唯一键账本串行跨失败记录的同键重放。
    修正的 routing_key/payload 只写入新 outbox 行，原故障内容保持可查。
    """
    row = await db.scalar(
        select(EventFailure).where(EventFailure.id == failure_id).with_for_update()
    )
    if row is None or row.replayed_at is not None:
        return False
    if not settings.message_bus_enabled:
        return False
    rk = row.routing_key if routing_key is None else routing_key
    body = dict(row.payload_json) if payload is None else dict(payload)
    if messaging.permanent_failure_reason(rk, {**body, "event_id": row.event_id}):
        return False
    # 同 id 已在途时视为已经安排投递，仍标记原故障记录已重放。
    queued = await enqueue_outbox(
        db, rk, body, event_id=row.event_id, replay=True
    )
    if not queued:
        # 活跃 outbox 行已在途，重复归档可安全摘除；其它情况保留审计副本。
        active = await db.scalar(
            select(OutboxMessage.id).where(
                OutboxMessage.event_id == row.event_id,
                OutboxMessage.status == OUTBOX_PENDING,
            )
        )
        if active is None:
            return False
    row.replayed_at = now_iso()
    await db.flush()
    return True
