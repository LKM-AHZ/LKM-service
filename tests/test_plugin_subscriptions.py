"""插件扩展点：新增订阅/routing_key 并挂到已有 worker 进程。"""

from __future__ import annotations

import pytest

from core import messaging, worker
from core.bus import topics


@pytest.fixture(autouse=True)
def _isolate_registries(monkeypatch: pytest.MonkeyPatch):
    """注册表是进程级可变状态：每个用例用独立副本，避免互相污染。"""
    subs = dict(topics.SUBSCRIPTIONS)
    monkeypatch.setattr(topics, "SUBSCRIPTIONS", subs)
    monkeypatch.setattr(messaging, "SUBSCRIPTIONS", subs)
    monkeypatch.setattr(topics, "ROUTING_KEY_TOPICS", dict(topics.ROUTING_KEY_TOPICS))
    monkeypatch.setattr(worker, "_EXTRA_SUBSCRIPTIONS", {})


def test_register_subscription_and_attach_to_worker() -> None:
    sub = topics.Subscription(
        "plugin-events", topics.TOPIC_CONTENT, (topics.RKEY_CONTENT_PUBLISHED,)
    )
    topics.register_subscription(sub)
    assert topics.SUBSCRIPTIONS["plugin-events"] is sub

    worker.register_worker_subscription("default", "plugin-events")
    worker.register_worker_subscription("default", "plugin-events")  # 幂等
    names = worker.subscriptions_for("default")

    assert names.count("plugin-events") == 1
    assert names[-1] == "plugin-events"
    assert names[0] == worker.JOBS_SUBSCRIPTION


def test_duplicate_subscription_with_different_definition_is_rejected() -> None:
    topics.register_subscription(
        topics.Subscription("dup", topics.TOPIC_CONTENT, (topics.RKEY_CONTENT_PUBLISHED,))
    )
    with pytest.raises(ValueError, match="定义不同"):
        topics.register_subscription(
            topics.Subscription("dup", topics.TOPIC_POINTS, (topics.RKEY_POINTS,))
        )


def test_subscription_with_unknown_routing_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知 routing_key"):
        topics.register_subscription(
            topics.Subscription("bad", topics.TOPIC_CONTENT, ("no.such.key",))
        )


def test_cross_namespace_subscription_is_rejected() -> None:
    with pytest.raises(ValueError, match="跨命名空间"):
        topics.register_subscription(
            topics.Subscription("bad-ns", topics.TOPIC_CONTENT, (topics.RKEY_SEND_CODE,))
        )


def test_plugin_can_register_new_routing_key_and_subscription() -> None:
    topic = topics.make_topic("biz", "plugin.events")
    topics.register_routing_key("event.plugin.changed", topic)
    topics.register_routing_key("event.plugin.changed", topic)  # 幂等

    topics.register_subscription(
        topics.Subscription("plugin-changed", topic, ("event.plugin.changed",))
    )
    worker.register_worker_subscription("content-index", "plugin-changed")

    assert "plugin-changed" in worker.subscriptions_for("content-index")


def test_routing_key_conflict_is_rejected() -> None:
    with pytest.raises(ValueError, match="已指向"):
        topics.register_routing_key(
            topics.RKEY_CONTENT_PUBLISHED, topics.TOPIC_POINTS
        )


def test_worker_attachment_validates_names() -> None:
    with pytest.raises(ValueError, match="未知 worker"):
        worker.register_worker_subscription("nobody", worker.JOBS_SUBSCRIPTION)
    with pytest.raises(ValueError, match="未登记"):
        worker.register_worker_subscription("default", "no-such-subscription")
    with pytest.raises(ValueError, match="未知 worker"):
        worker.subscriptions_for("nobody")
