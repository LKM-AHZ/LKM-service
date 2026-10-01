"""
死信消费者：消费 Pulsar ``system/dlq`` 订阅，把每条死信落库 dlq_messages 供人工重投/审计。
Pulsar DeadLetterPolicy 在消费失败重投超限后把消息投到 ``persistent://lkm/system/dlq``；
本订阅消费并落库（落库成功即 ack 移出 broker，后续从 DB 治理）。重投走 admin 端点
入队 outbox，由 relay 发布回原 routing_key。
死信消息的 routing_key 从消息 properties 还原（发布时写入），attempts 取 Pulsar
``redelivery_count``。DLQ 订阅不配置二次死信策略，落库失败继续负确认等待重投。
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import messaging, metrics_relay
from core.config import settings
from core.db.base import now_iso
from core.db.dlq import DlqMessage
from core.db.outbox import OUTBOX_PENDING, OutboxMessage, enqueue_outbox
from core.db.session import new_worker_session as new_session
from core.tracing import setup_tracing

logger = logging.getLogger("lkm.worker_dlq")


def _make_model(
    *,
    routing_key: str,
    payload: dict[str, Any],
    attempts: int,
    reason: str,
    status: str,
    topic: str,
    source_message_id: str | None = None,
) -> DlqMessage:
    """把一条死信消息映射为 DlqMessage。"""
    if not routing_key:
        # 不拿 topic 顶替：DLQ topic 不是合法 routing_key，重投只会以「未知 routing_key」
        # 失败，且列表里看不出这条根本不可重投。缺失即落显式哨兵 + 告警。
        logger.warning("死信缺少 routing_key property topic=%s（标记为不可重投）", topic)
    return DlqMessage(
        routing_key=routing_key or "unknown",
        payload_json={"payload": payload},
        exchange=topic,
        attempts=attempts,
        reason=reason[:255],
        status=status,
        # DlqMessage 的约定是 UTCDateTime + now_iso()（见其 docstring「勿用 datetime.now(UTC)」）
        created_at=now_iso(),
        source_message_id=source_message_id,
    )


async def _persist(model: DlqMessage) -> None:
    """把一条 DLQ 模型落库（独立事务，与请求上下文解耦）。"""
    db = await new_session()
    try:
        db.add(model)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if model.source_message_id is None or await db.scalar(
            select(DlqMessage.id).where(
                DlqMessage.source_message_id == model.source_message_id
            )
        ) is None:
            raise
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def requeue(
    db: Any,
    dlq_id: uuid.UUID,
    *,
    routing_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """把 pending 死信与 outbox 行同事务落库，供 relay 可靠重投。"""
    # 行锁让两个并发人工请求串行检查 pending 状态；后到者看到 requeued 后不再入队。
    m = await db.scalar(
        select(DlqMessage).where(DlqMessage.id == dlq_id).with_for_update()
    )
    if m is None or m.status != "pending":
        return False
    # 缺 payload / payload 非对象：不能退化成「重投一个空事件」（消费端会拒收或空跑），
    # 如实拒绝并留日志，让这条坏行可见
    parsed = (m.payload_json or {}).get("payload") if payload is None else payload
    if not isinstance(parsed, dict):
        logger.error("死信 payload 缺失/非法 id=%s，拒绝重投", dlq_id)
        return False
    if not settings.message_bus_enabled:
        return False
    rk = m.routing_key if routing_key is None else routing_key
    if messaging.permanent_failure_reason(rk, parsed) is not None:
        logger.error("死信 routing_key/payload 无效 id=%s，拒绝重投", dlq_id)
        return False
    event_id = parsed.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        event_id = m.id.hex
    queued = await enqueue_outbox(
        db, rk, parsed, event_id=event_id, replay=True
    )
    if not queued:
        active = await db.scalar(
            select(OutboxMessage.id).where(
                OutboxMessage.event_id == event_id,
                OutboxMessage.status == OUTBOX_PENDING,
            )
        )
        if active is None:
            return False
    m.status = "requeued"
    m.requeued_at = now_iso()
    await db.commit()
    return True


async def _on_dlq(payload: dict[str, Any], meta: messaging.MessageMeta) -> None:
    """死信消息回调：落库（失败抛出 → 负确认，Pulsar 重投）。"""
    model = _make_model(
        routing_key=meta.properties.get("routing_key", ""),
        payload=payload,
        attempts=meta.redelivery_count,
        reason="dead-lettered",
        status="pending",
        topic=meta.topic,
        source_message_id=meta.message_id,
    )
    await _persist(model)
    logger.info("死信落库 routing_key=%s attempts=%s", model.routing_key, model.attempts)


async def consume_dlq() -> None:
    """DLQ 消费者主循环（进程入口在 ``boot.workers.dlq``，那里先装配再调用本函数）。"""
    # 非 ASGI 进程：初始化 provider 才能导出消费 span（默认关时 no-op）
    setup_tracing(service_suffix="-dlq")
    # 跨进程指标中继：人工重投走的 messaging.publish 会写 notify_failed_total，
    # 而本进程不暴露 /metrics——快照交给 API 进程代报。
    metrics_relay.start_publisher()
    await messaging.run_subscription(messaging.SUB_DLQ.name, _on_dlq)
