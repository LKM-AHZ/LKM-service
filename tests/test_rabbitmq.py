"""RabbitMQ adapter contract without a live broker."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from core import messaging, pulsar_lag, rabbitmq
from core.config import settings


async def test_publish_selects_rabbitmq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "message_bus", "rabbitmq")
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://test:secret@localhost/")
    publish = AsyncMock()
    monkeypatch.setattr(rabbitmq, "publish", publish)
    assert await messaging.publish(
        messaging.RKEY_NOTIFY, {"fn": "notify_upload", "args": ["u1"]}
    )
    assert publish.await_count == 1
    topic, data, props = publish.await_args.args
    assert topic == messaging.TOPIC_NOTIFY
    assert json.loads(data)["fn"] == "notify_upload"
    assert props["routing_key"] == messaging.RKEY_NOTIFY


async def test_health_uses_selected_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "message_bus", "rabbitmq")
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://test:secret@localhost/")
    monkeypatch.setattr(settings, "pulsar_admin_url", "")
    probe = AsyncMock(return_value=("up", None))
    monkeypatch.setattr(rabbitmq, "probe_health", probe)
    assert await pulsar_lag.probe_health() == ("up", None)
    probe.assert_awaited_once()


async def test_topology_predeclares_every_points_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_dlq_max_redeliver", 1)

    class Queue:
        def __init__(self, name: str) -> None:
            self.name = name
            self.bindings: list[str] = []

        async def bind(self, exchange: object) -> None:
            self.bindings.append(exchange.name)  # type: ignore[attr-defined]

    class Exchange:
        def __init__(self, name: str) -> None:
            self.name = name

    class Channel:
        def __init__(self) -> None:
            self.queues: dict[str, tuple[Queue, dict[str, object]]] = {}

        async def declare_exchange(self, name: str, *_args: object, **_kwargs: object):
            return Exchange(name)

        async def declare_queue(self, name: str, **kwargs: object):
            queue = Queue(name)
            self.queues[name] = (queue, kwargs)
            return queue

    channel = Channel()
    await rabbitmq._topology(channel, messaging.TOPIC_POINTS)
    names = {
        f"lkm.{sub.name}"
        for sub in messaging.SUBSCRIPTIONS.values()
        if sub.topic == messaging.TOPIC_POINTS
    }
    assert names <= channel.queues.keys()
    for name in names:
        queue, kwargs = channel.queues[name]
        assert queue.bindings == ["lkm.biz.points.apply"]
        assert kwargs["arguments"]["x-delivery-limit"] == 1  # type: ignore[index]
    assert "lkm.dlq-persist" in channel.queues


async def test_failed_delivery_requeues_and_dlq_keeps_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Message:
        body = b'{"fn":"apply_point","args":["u1"]}'
        message_id = "event-1"

        def __init__(self) -> None:
            self.headers = {"routing_key": messaging.RKEY_POINTS, "x-delivery-count": 1}
            self.ack = AsyncMock()
            self.reject = AsyncMock()

    async def fail(*_args: object) -> None:
        raise RuntimeError("handler failure")

    message = Message()
    await rabbitmq._consume_message(message, messaging.SUB_POINTS_STATS, fail)
    message.reject.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()

    seen: list[messaging.MessageMeta] = []

    async def persist(_payload: dict[str, object], meta: messaging.MessageMeta) -> None:
        seen.append(meta)

    message = Message()
    message.headers = {
        "routing_key": messaging.RKEY_POINTS,
        "x-death": [{"queue": "lkm.points-stats"}],
    }
    await rabbitmq._consume_message(message, messaging.SUB_DLQ, persist)
    message.ack.assert_awaited_once()
    assert seen[0].message_id == "event-1:lkm.points-stats"
    assert seen[0].redelivery_count >= 1
