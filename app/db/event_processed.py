"""消费者幂等去重表（M1.3；M4 扩为 (scope, event_id) 复合键）。

全局 event_id（outbox_events.event_id → 透传到消费端 payload）作幂等键：消费者 handler
成功执行后把该 event_id 落此表；重放同一事件（relay 故障重投 / DLQ requeue 再投）消费端
按 event_id 查到已处理即 ack 跳过，避免二次副作用 —— 达成 at-least-once 下的去重收口。

**M4 scope 维度**：迁移 Pulsar 后同一事件可由多个订阅消费（points 拆 reward/stats/tasks
三个 subscription 扇出）。若仍以 event_id 为全局主键，首个订阅记账后其余订阅会被误判
"已处理"而跳过，静默丢副作用。故幂等键改为 ``(scope, event_id)``，``scope`` 取订阅名
（如 points-stats），各订阅独立记账、互不跳过；单订阅内的重放仍按 event_id 去重。

未命中（首次）由 handler 自身保证单次副作用（points 另有 ref 幂等作次级守约）；本表是
队列层幂等框架的主键，跨 M2/M3/M4 消费可靠事件均复用。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, now_iso

logger = logging.getLogger(__name__)

# 未显式指定订阅名的调用（兼容旧直发/单订阅路径）落到此 scope，与具名订阅隔离。
DEFAULT_SCOPE = "default"


class EventProcessed(Base):
    """消费端已处理的事件幂等账本，(scope, event_id) 复合主键去重。"""

    __tablename__: str = "event_processed"

    # scope = 订阅名（消费隔离单元）。复合主键提供唯一约束：并发重复 INSERT 由 DB 唯一性
    # 兜底（先查后插仍可能少数并发重复，主键到层最好）。
    scope: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=DEFAULT_SCOPE
    )
    # 幂等键 = outbox 全局 event_id（36 位 hex）
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # handler 首次成功执行的时间（审计/清理用）
    processed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


async def already_processed(
    db: AsyncSession, event_id: str, *, scope: str = DEFAULT_SCOPE
) -> bool:
    """该 (scope, event_id) 是否已有成功处理记录（命中 → 重放应跳过）。"""
    eid = await db.scalar(
        select(EventProcessed.event_id).where(
            EventProcessed.scope == scope, EventProcessed.event_id == event_id
        )
    )
    return eid is not None


async def record_processed(
    db: AsyncSession, event_id: str, *, scope: str = DEFAULT_SCOPE
) -> bool:
    """记录一笔成功处理；并发下撞主键忽略、返回是否新增。

    注意：本函数**拥有事务边界**——会对传入的 ``db`` 执行 commit，撞主键时执行 rollback，
    故调用方不得在同一 session 上挂载其它待提交的变更（现唯一调用方 worker 的
    ``_dispatch_with_dedup`` 传的是专用会话）。
    """
    db.add(EventProcessed(scope=scope, event_id=event_id))
    try:
        await db.commit()
        return True
    except IntegrityError:
        # 并发重复标记：另一消费者已抢先落账，视为已记账不报错。
        # 但 IntegrityError 也可能是**别的**约束失败（NOT NULL/FK/CHECK 等）：一律当
        # 「已记账」会让消费者误跳过该事件的副作用，故回滚后复查——行确实在才算重复，
        # 否则原样抛出，让失败走重投/DLQ 而不是静默丢副作用。
        await db.rollback()
        if await already_processed(db, event_id, scope=scope):
            return False
        raise
