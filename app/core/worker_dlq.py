"""死信消费者：消费 Pulsar ``system/dlq`` 订阅，把每条死信落库 dlq_messages 供人工重投/审计。

Pulsar DeadLetterPolicy 在消费失败重投超限后把消息投到 ``persistent://lkm/system/dlq``；
本订阅消费并落库（落库成功即 ack 移出 broker，后续从 DB 治理）。重投走 admin 端点
re-publish 回原 routing_key。

死信消息的 routing_key 从消息 properties 还原（发布时写入），attempts 取 Pulsar
``redelivery_count``。DLQ topic 的消费自身不再触发死信转发（Pulsar 不会对已死信消息
二次投死信），故无循环风险。
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core import messaging
from app.core.tracing import setup_tracing
from app.db.session import new_session
from app.modules.admin.models import DlqMessage

logger = logging.getLogger("lkm.worker_dlq")


def _make_model(
    *,
    routing_key: str,
    payload: dict[str, Any],
    attempts: int,
    reason: str,
    status: str,
    topic: str,
) -> DlqMessage:
    """把一条死信消息映射为 DlqMessage。"""
    return DlqMessage(
        routing_key=routing_key or topic or "unknown",
        payload_json={"payload": payload},
        exchange=topic,
        attempts=attempts,
        reason=reason[:255],
        status=status,
        created_at=datetime.now(UTC),
    )


async def _persist(model: DlqMessage) -> None:
    """把一条 DLQ 模型落库（独立事务，与请求上下文解耦）。"""
    db = await new_session()
    try:
        db.add(model)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def requeue(db: Any, dlq_id: uuid.UUID) -> bool:
    """把一条 pending 死信 re-publish 回原 routing_key，标记 requeued。"""
    m = await db.scalar(select(DlqMessage).where(DlqMessage.id == dlq_id))
    if m is None or m.status != "pending":
        return False
    parsed = m.payload_json.get("payload", {})
    ok = await messaging.publish(m.routing_key, parsed)
    if ok:
        m.status = "requeued"
        m.requeued_at = datetime.now(UTC)
        await db.commit()
    return ok


async def _on_dlq(payload: dict[str, Any], meta: messaging.MessageMeta) -> None:
    """死信消息回调：落库（失败抛出 → 负确认，Pulsar 重投）。"""
    model = _make_model(
        routing_key=meta.properties.get("routing_key", ""),
        payload=payload,
        attempts=meta.redelivery_count,
        reason="dead-lettered",
        status="pending",
        topic=meta.topic,
    )
    await _persist(model)
    logger.info("死信落库 routing_key=%s attempts=%s", model.routing_key, model.attempts)


async def consume_dlq() -> None:
    """DLQ 消费者主循环（compose worker-dlq 入口）。"""
    # 非 ASGI 进程：初始化 provider 才能导出消费 span（默认关时 no-op）
    setup_tracing(service_suffix="-dlq")
    await messaging.run_subscription(messaging.SUB_DLQ.name, _on_dlq)


if __name__ == "__main__":
    asyncio.run(consume_dlq())
