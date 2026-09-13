"""M4 消息总线抽象测试：映射表、transport 发布、fail-open、JSON schema（无需真实 broker）。"""

import json
from collections.abc import Iterator

import pytest
from prometheus_client import REGISTRY

from app.core import messaging
from tests.fakes import InMemoryTransport


@pytest.fixture(autouse=True)
def _reset_transport() -> Iterator[None]:
    messaging.set_transport(None)
    yield
    messaging.set_transport(None)


def _failed_count() -> float:
    value = REGISTRY.get_sample_value("notify_failed_total")
    return value if value is not None else 0.0


async def test_publish_routes_to_mapped_topic() -> None:
    transport = InMemoryTransport()
    messaging.set_transport(transport)
    ok = await messaging.publish(
        messaging.RKEY_NOTIFY, {"fn": "notify_upload", "args": ["u1"]}
    )
    assert ok is True
    topic, data, props = transport.published[0]
    assert topic == messaging.TOPIC_NOTIFY
    assert props["routing_key"] == messaging.RKEY_NOTIFY
    assert props["fn"] == "notify_upload"
    assert json.loads(data) == {"fn": "notify_upload", "args": ["u1"]}


async def test_publish_transport_failure_is_fail_open() -> None:
    transport = InMemoryTransport(fail=True)
    messaging.set_transport(transport)
    before = _failed_count()
    ok = await messaging.publish(messaging.RKEY_POINTS, {"fn": "apply_point_event"})
    assert ok is False
    assert _failed_count() == before + 1


async def test_publish_unconfigured_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(messaging.settings, "pulsar_url", "")
    before = _failed_count()
    ok = await messaging.publish(messaging.RKEY_NOTIFY, {"fn": "notify_upload"})
    assert ok is False
    # 未配置 fail-open 不计数（对齐迁移前「ch None 不计」语义）
    assert _failed_count() == before


async def test_publish_unknown_routing_key_returns_false() -> None:
    transport = InMemoryTransport()
    messaging.set_transport(transport)
    assert await messaging.publish("event.nope", {"fn": "x"}) is False
    assert transport.published == []


async def test_publish_uses_pulsar_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入 transport 且已配置时，走 Pulsar producer 且 send 带 routing_key 属性。"""
    monkeypatch.setattr(messaging.settings, "pulsar_url", "pulsar://h:6650")
    sent: list[tuple] = []

    class _FakeProducer:
        def send(self, data: bytes, properties: dict[str, str]) -> None:
            sent.append(("send", data, properties))

    async def _fake_get_producer(topic: str) -> _FakeProducer:
        sent.append(("topic", topic))
        return _FakeProducer()

    monkeypatch.setattr(messaging, "_get_producer", _fake_get_producer)
    ok = await messaging.publish(
        messaging.RKEY_SEND_CODE, {"fn": "send_code", "args": [1]}
    )
    assert ok is True
    assert sent[0] == ("topic", messaging.TOPIC_EMAIL)
    _, data, properties = sent[1]
    assert properties["routing_key"] == messaging.RKEY_SEND_CODE
    assert properties["fn"] == "send_code"
    assert json.loads(data) == {"fn": "send_code", "args": [1]}


def test_event_schema_roundtrip() -> None:
    schema = messaging.make_event_schema(messaging.TOPIC_POINTS)
    payload = {"fn": "apply_point_event", "args": [7, "post", "p1"], "event_id": "e1"}
    assert schema.decode(schema.encode(payload)) == payload


def test_subscription_index_unique_and_points_fanout() -> None:
    assert len(messaging.SUBSCRIPTIONS) == len(
        {s.name for s in messaging.SUBSCRIPTIONS.values()}
    )
    points_topics = {
        messaging.SUB_POINTS_REWARD.topic,
        messaging.SUB_POINTS_STATS.topic,
        messaging.SUB_POINTS_TASKS.topic,
    }
    assert len(points_topics) == 1
