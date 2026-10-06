"""
死信消费者：消费 ``system/dlq`` 订阅，把每条死信落库 dlq_messages 供人工重投/审计。
所选 broker 在消费失败重投超限后把消息投到 ``persistent://lkm/system/dlq``；
本订阅消费并落库（落库成功即 ack 移出 broker，后续从 DB 治理）。重投走 admin 端点
入队 outbox，由 relay 发布回原 routing_key。
死信消息的 routing_key 从消息 properties 还原（发布时写入），attempts 取
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
        logger.warning("死信缺少 routing_key property topic=%s（标记为不可重投）", topic)
    return DlqMessage(
        routing_key=routing_key or "unknown",
        payload_json={"payload": payload},
        exchange=topic,
        attempts=attempts,
        reason=reason[:255],
        status=status,
        # 使用模型约定的 UTC 时间格式。
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
    # 行锁串行处理并发重投请求。
    m = await db.scalar(
        select(DlqMessage).where(DlqMessage.id == dlq_id).with_for_update()
    )
    if m is None or m.status != "pending":
        return False
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
    """死信消息回调：落库（失败抛出 → 负确认，broker 重投）。"""
    model = _make_model(
        routing_key=meta.properties.get("routing_key", ""),
        payload=payload,
        attempts=meta.redelivery_count,
        reason=(
            "invalid envelope"
            if messaging.RAW_MESSAGE_KEY in payload
            else "dead-lettered"
        ),
        status="pending",
        topic=meta.topic,
        source_message_id=meta.message_id,
    )
    await _persist(model)
    logger.info("死信落库 routing_key=%s attempts=%s", model.routing_key, model.attempts)


async def consume_dlq() -> None:
    """DLQ 消费者主循环（进程入口在 ``boot.workers.dlq``，那里先装配再调用本函数）。"""
    # 初始化死信 worker 的追踪。
    setup_tracing(service_suffix="-dlq")
    # 指标快照交给 API 进程上报。
    metrics_relay.start_publisher()
    await messaging.run_subscription(messaging.SUB_DLQ.name, _on_dlq)
