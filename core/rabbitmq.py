"""RabbitMQ implementation of the existing messaging topic/subscription contract."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aio_pika

from core.config import settings
from core.secrets import reveal

if TYPE_CHECKING:
    from core.messaging import MessageHandler, Subscription

logger = logging.getLogger("lkm.rabbitmq")

_connection: Any = None
_channel: Any = None
_ready_topics: set[str] = set()
_lock = asyncio.Lock()


def _exchange_name(topic: str) -> str:
    return topic.removeprefix("persistent://").replace("/", ".")


def _queue_name(subscription: str) -> str:
    return f"{settings.pulsar_tenant}.{subscription}"


async def _topology(channel: Any, topic: str) -> Any:
    """Declare every queue before publishing, including fanout subscribers not yet running."""
    from core.messaging import SUB_DLQ, SUBSCRIPTIONS, TOPIC_DLQ

    dlx = await channel.declare_exchange(
        _exchange_name(TOPIC_DLQ), aio_pika.ExchangeType.FANOUT, durable=True
    )
    dlq = await channel.declare_queue(
        _queue_name(SUB_DLQ.name), durable=True, arguments={"x-queue-type": "quorum"}
    )
    await dlq.bind(dlx)
    if topic == TOPIC_DLQ:
        return dlx

    exchange = await channel.declare_exchange(
        _exchange_name(topic), aio_pika.ExchangeType.FANOUT, durable=True
    )
    for sub in SUBSCRIPTIONS.values():
        if sub.topic != topic or sub.name == SUB_DLQ.name:
            continue
        queue = await channel.declare_queue(
            _queue_name(sub.name),
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": settings.pulsar_dlq_max_redeliver,
                "x-dead-letter-exchange": _exchange_name(TOPIC_DLQ),
            },
        )
        await queue.bind(exchange)
    return exchange


async def _publisher() -> Any:
    global _connection, _channel
    if _channel is None or _channel.is_closed:
        if _connection is not None:
            with suppress(Exception):
                await _connection.close()
        _connection = await aio_pika.connect_robust(reveal(settings.rabbitmq_url))
        _channel = await _connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        _ready_topics.clear()
    return _channel


async def publish(topic: str, data: bytes, props: dict[str, str]) -> None:
    async with _lock:
        channel = await _publisher()
        if topic not in _ready_topics:
            await _topology(channel, topic)
            _ready_topics.add(topic)
        exchange = await channel.get_exchange(_exchange_name(topic))
        await exchange.publish(
            aio_pika.Message(
                body=data,
                headers=dict[str, Any](props),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=uuid.uuid4().hex,
            ),
            routing_key="",
            mandatory=True,
        )


async def _consume_message(
    message: Any, sub: Subscription, handler: MessageHandler
) -> None:
    from core.messaging import (
        JOB_TIMEOUT_S,
        RAW_MESSAGE_KEY,
        SUB_DLQ,
        MessageMeta,
        _run_handler,
    )

    try:
        payload = json.loads(message.body)
        if not isinstance(payload, dict):
            raise ValueError("payload 非 JSON 对象")
    except (TypeError, ValueError):
        if sub.name != SUB_DLQ.name:
            logger.warning("非法消息重投后转死信 subscription=%s", sub.name)
            await message.reject(requeue=True)
            return
        payload = {RAW_MESSAGE_KEY: base64.b64encode(message.body).decode("ascii")}

    headers = dict(message.headers or {})
    props = {str(k): str(v) for k, v in headers.items() if isinstance(v, str)}
    source_id = message.message_id
    redelivery_count = int(headers.get("x-delivery-count", 0))
    if sub.name == SUB_DLQ.name and source_id:
        deaths = headers.get("x-death") or []
        if deaths:
            source_id = f"{source_id}:{deaths[0].get('queue', '')}"
            redelivery_count = max(redelivery_count, settings.pulsar_dlq_max_redeliver)
    meta = MessageMeta(
        topic=sub.topic,
        subscription=sub.name,
        properties=props,
        redelivery_count=redelivery_count,
        message_id=source_id,
    )
    try:
        await asyncio.wait_for(_run_handler(handler, payload, meta), JOB_TIMEOUT_S)
    except Exception:
        logger.exception("消费失败→重投 subscription=%s", sub.name)
        await message.reject(requeue=True)
        return
    await message.ack()


async def run_subscription(sub: Subscription, handler: MessageHandler) -> None:
    while True:
        connection: Any = None
        try:
            connection = await aio_pika.connect_robust(reveal(settings.rabbitmq_url))
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            await _topology(channel, sub.topic)
            queue = await channel.get_queue(_queue_name(sub.name))
            async with queue.iterator() as messages:
                async for message in messages:
                    await _consume_message(message, sub, handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("RabbitMQ 订阅失败 subscription=%s；退避后重连", sub.name)
            await asyncio.sleep(2)
        finally:
            if connection is not None:
                with suppress(Exception):
                    await connection.close()


async def probe_health(timeout_s: float) -> tuple[str, str | None]:
    try:
        connection = await asyncio.wait_for(
            aio_pika.connect(reveal(settings.rabbitmq_url), timeout=timeout_s),
            timeout_s,
        )
        await connection.close()
    except Exception as exc:
        return "error", f"rabbitmq 不可达: {exc}"
    return "up", None


async def collect_backlog() -> dict[str, int]:
    """Passive queue declarations return broker message counts without changing topology."""
    from core.messaging import SUBSCRIPTIONS

    connection = await aio_pika.connect(reveal(settings.rabbitmq_url))
    try:
        channel = await connection.channel()
        result: dict[str, int] = {}
        for sub in SUBSCRIPTIONS.values():
            queue = await channel.declare_queue(_queue_name(sub.name), passive=True)
            result[sub.name] = queue.declaration_result.message_count or 0
        return result
    finally:
        await connection.close()


async def close() -> None:
    global _connection, _channel
    async with _lock:
        connection, _connection = _connection, None
        _channel = None
        _ready_topics.clear()
        if connection is not None:
            with suppress(Exception):
                await connection.close()
