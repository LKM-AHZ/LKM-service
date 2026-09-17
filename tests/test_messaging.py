"""M4 消息总线抽象测试：映射表、transport 发布、fail-open、JSON schema（无需真实 broker）。"""

import json
import logging
import sys
import threading
import types
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


def _client_kwargs_with_timeout(monkeypatch: pytest.MonkeyPatch, timeout: float) -> dict:
    """用假 pulsar 模块截获 Client 构造参数（不连真 broker）。"""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, url: str, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "pulsar", types.SimpleNamespace(Client=_FakeClient))
    monkeypatch.setattr(messaging, "_client", None)
    monkeypatch.setattr(messaging.settings, "pulsar_operation_timeout_s", timeout)
    messaging._client_locked()
    return captured


def test_client_operation_timeout_is_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：`operation_timeout_seconds` 必须是 int。

    Settings 里该字段是 float（默认 30.0），曾原样传给 Pulsar Python 客户端 →
    `ValueError: Argument operation_timeout_seconds is expected to be of type 'int' and
    not 'float'` → 所有 worker/producer 都建不出 client。compose 与 k8s 同一镜像均中招，
    只是当时 outbox 为空、影响潜伏（2026-09-17 在 k8s 真机验收中定位）。
    """
    kwargs = _client_kwargs_with_timeout(monkeypatch, 30.0)
    value = kwargs["operation_timeout_seconds"]
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value == 30


def test_client_operation_timeout_clamped_to_at_least_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """亚秒配置取整后会变 0，而该参数为 0 等于「无/瞬时超时」——故下限钳到 1。"""
    kwargs = _client_kwargs_with_timeout(monkeypatch, 0.4)
    assert kwargs["operation_timeout_seconds"] == 1


def test_receive_timeout_is_not_logged_as_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """回归：长轮询到期（`pulsar.Timeout`）是正常路径，不得打 ERROR 整栈。

    修好 `operation_timeout_seconds` 类型 bug 后 worker 才真正走到消费循环，随即暴露：
    `receive(timeout_millis=1000)` 的到期异常落进 `except Exception` 分支，每个 worker
    每秒一条 traceback（实测 30 秒 15 条）→ 刷爆日志并灌进 ClickHouse `app_logs`。
    """
    import pulsar

    stop = threading.Event()
    calls = {"n": 0}

    class _FakeConsumer:
        def receive(self, timeout_millis: int = 0) -> object:
            calls["n"] += 1
            if calls["n"] > 3:
                stop.set()  # 让循环自然退出
            raise pulsar.Timeout()

        def close(self) -> None: ...

    monkeypatch.setattr(messaging, "_create_consumer_sync", lambda _sub: _FakeConsumer())

    with caplog.at_level(logging.ERROR, logger="lkm.messaging"):
        messaging._receive_loop(
            messaging.SUB_POINTS_STATS, lambda *_: None, None, stop  # type: ignore[arg-type]
        )

    assert calls["n"] == 4
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
