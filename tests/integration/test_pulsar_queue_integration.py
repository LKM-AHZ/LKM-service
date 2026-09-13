"""Pulsar 集成测试：真实 broker 下 publish → 订阅消费、points 三订阅扇出。

默认排除（pytest 默认 ``-m "not integration"``）。需 ``LKM_PULSAR_URL`` 指向可用 Pulsar
（compose 的 pulsar standalone，或本地 standalone），否则 skip。

前置：``lkm`` 租户与 ``biz/auth/system`` namespace 已建（生产由 compose ``pulsar-init``
完成，本地可 `bin/pulsar-admin tenants create lkm` + `namespaces create`）。
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from app.core import messaging
from app.core.config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def pulsar_url() -> str:
    url = os.environ.get("LKM_PULSAR_URL", "")
    if not url:
        pytest.skip("LKM_PULSAR_URL 为空")
    return url


@pytest.fixture
async def _messaging_on(
    pulsar_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """指向真实 broker 并复位客户端单例（避免复用旧 url 的缓存 client）。"""
    await messaging.close()
    monkeypatch.setattr(settings, "pulsar_url", pulsar_url)
    yield
    await messaging.close()


async def test_publish_then_consume(_messaging_on: None, pulsar_url: str) -> None:
    """messaging.publish 落到映射 topic，独立订阅可收到。"""
    import pulsar

    client = pulsar.Client(pulsar_url)
    sub = f"it-{uuid.uuid4().hex[:8]}"
    consumer = client.subscribe(
        messaging.TOPIC_NOTIFY,
        sub,
        schema=messaging.make_event_schema(messaging.TOPIC_NOTIFY),
    )
    try:
        ok = await messaging.publish(
            messaging.RKEY_NOTIFY, {"fn": "notify_upload", "args": ["it-upload"]}
        )
        assert ok is True
        msg = await asyncio.to_thread(consumer.receive, 15000)
        assert json.loads(msg.data())["args"] == ["it-upload"]
        consumer.acknowledge(msg)
    finally:
        consumer.close()
        client.close()


async def test_points_fanout_three_subscriptions(
    _messaging_on: None, pulsar_url: str
) -> None:
    """同一 biz/points.apply 事件被 reward/stats/tasks 三个订阅各消费一次（扇出验收）。"""
    import pulsar

    client = pulsar.Client(pulsar_url)
    names = ["points-reward", "points-stats", "points-tasks"]
    consumers = [
        client.subscribe(
            messaging.TOPIC_POINTS,
            f"it-{n}-{uuid.uuid4().hex[:8]}",
            schema=messaging.make_event_schema(messaging.TOPIC_POINTS),
        )
        for n in names
    ]
    try:
        ok = await messaging.publish(
            messaging.RKEY_POINTS,
            {"fn": "apply_point_event", "args": [7, "post", "it:p1"]},
        )
        assert ok is True
        for consumer in consumers:
            msg = await asyncio.to_thread(consumer.receive, 15000)
            assert json.loads(msg.data())["args"] == [7, "post", "it:p1"]
            consumer.acknowledge(msg)
    finally:
        for consumer in consumers:
            consumer.close()
        client.close()
