"""
发布路径与 broker 连接生命周期。

一次发布 = 编码 → 事件契约校验 → 组装 properties → 选 transport → 投递。

发布/消费哲学沿用旧 ``amqp`` 层：**fail-open**——未配置 broker 或投递异常时不阻塞请求
（返回 False 由调用方降级），异常计数 ``notify_failed_total``。

传输选择（本层是唯一选点）：
1. 测试注入的 ``_transport``（``set_transport``）优先；
2. ``settings.message_bus == "rabbitmq"`` → ``core.rabbitmq``；
3. 否则 Pulsar：producer 懒建缓存，``producer.send`` 经 ``asyncio.to_thread``
   （官方 Python 客户端是同步阻塞 API）。

Pulsar 的 client/producer 缓存与「已关闭」标记留在本模块：它是默认传输实现的内部状态，
消费侧（``core.bus.consumer``）也经 ``runtime._get_client_sync`` 复用同一 client。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from core import tracing
from core.bus.contracts import _contract_violations, _record_violation
from core.bus.schema import _encode_event, make_event_schema
from core.bus.topics import ROUTING_KEY_TOPICS
from core.config import settings
from core.metrics import notify_failed_total

logger = logging.getLogger("lkm.messaging")

# 单任务执行上限（秒）：与迁移前 worker.JOB_TIMEOUT_S 对齐，超时视为消费失败 → 负确认。
JOB_TIMEOUT_S = 120


@dataclass(frozen=True)
class MessageMeta:
    """
    消费消息的元数据（业务 payload 之外的随消息信息）。
    - ``routing_key`` / ``fn`` 从发布时写入的 properties 还原（死信落库需 routing_key）。
    - ``redelivery_count``：Pulsar 重投次数，死信落库作 attempts。
    """

    topic: str
    subscription: str
    properties: dict[str, str]
    redelivery_count: int = 0
    message_id: str | None = None


# 消费回调类型：async 函数，返回 coroutine（供 run_coroutine_threadsafe 调度）。
MessageHandler = Callable[[dict[str, Any], MessageMeta], Coroutine[Any, Any, None]]

# DLQ 无二次死信策略；无法解成对象的原始消息用 base64 包装后交给 DLQ handler
# 持久化，避免在源订阅直接 ack 丢弃，或在 DLQ 上无限负确认。
RAW_MESSAGE_KEY = "_raw_message_base64"


class Transport(Protocol):
    """发布 transport 测试 seam：内存替身经 ``set_transport`` 注入。"""

    async def publish(self, topic: str, data: bytes, props: dict[str, str]) -> None: ...


_transport: Transport | None = None
_client: Any = None
_producers: dict[str, Any] = {}
# 已关闭标记：close() 只清缓存不是终态，关闭后并发中的 publish/消费重连仍会惰性新建 client，
# 把 broker 连接与后台线程带到 shutdown 之后。
_closed = False
_client_lock = threading.Lock()


def set_transport(transport: Transport | None) -> None:
    """
    注入/清除发布 transport（测试用；None 恢复真实 Pulsar 路径）。
    同时复位「已关闭」标记：close() 兼作测试复位，注入新 transport 即代表新一轮使用。
    """
    global _transport, _closed
    _transport = transport
    _closed = False


def _client_locked() -> Any:
    """取/建单例 Pulsar Client；调用方须持 ``_client_lock``。"""
    global _client
    if _closed:
        raise RuntimeError("message bus is shut down")
    if _client is None:
        import pulsar

        _client = pulsar.Client(
            settings.pulsar_url,
            operation_timeout_seconds=max(1, int(settings.pulsar_operation_timeout_s)),
        )
    return _client


def _get_client_sync() -> Any:
    with _client_lock:
        return _client_locked()


def _create_producer_cached(topic: str) -> Any:
    """
    取/建该 topic 的 producer（缓存去重；须在线程中调用）。
    阻塞的 ``create_producer``（含 broker 侧 schema 注册/兼容性检查，可能卡数秒）刻意
    放在锁外：``_create_consumer_sync`` 取 client 要用同一把锁，若持锁建 producer，
    一个慢 producer 会把所有订阅的建连与重建一起堵死（消费线程集体停摆）。
    """
    producer = _producers.get(topic)
    if producer is not None:
        return producer
    client = _get_client_sync()  # 短临界区：只取/建 client
    producer = client.create_producer(topic, schema=make_event_schema(topic))
    with _client_lock:
        # Producer construction runs outside the lock. Shutdown may have
        # cleared and closed this client while construction was in progress.
        cached = (
            None
            if _closed or _client is not client
            else _producers.setdefault(topic, producer)
        )
    if cached is not producer:
        with suppress(Exception):
            producer.close()
    if cached is None:
        raise RuntimeError("message bus closed while creating producer")
    return cached


async def _get_producer(topic: str) -> Any:
    producer = _producers.get(topic)
    if producer is not None:
        return producer
    return await asyncio.to_thread(_create_producer_cached, topic)


async def publish(routing_key: str, payload: Mapping[str, Any]) -> bool:
    """
    发布一条事件到 routing_key 对应 topic。fail-open：不可用/异常 → False。
    - transport 已注入（测试）→ 走替身，异常计 ``notify_failed_total``。
    - 未配置消息总线（pulsar_url 空）→ False 不计数（对齐迁移前 ch None 语义）。
    - 真实 Pulsar：producer 懒建缓存，``send`` 经 ``asyncio.to_thread``（同步 API 不阻塞循环）。
    """
    topic = ROUTING_KEY_TOPICS.get(routing_key)
    if topic is None:
        logger.error("未知 routing_key=%s，丢弃发布", routing_key)
        return False
    if _closed:
        logger.warning("消息总线已关闭，丢弃发布 routing_key=%s", routing_key)
        return False

    # 发布 span：未启用 tracing 时为 no-op；traceparent 注入 props 供消费端续链
    with tracing.tracer("lkm.messaging").start_as_current_span(
        f"{settings.message_bus}.publish"
    ) as span:
        with suppress(Exception):
            span.set_attribute("messaging.system", settings.message_bus)
            span.set_attribute("messaging.destination.name", topic)
            span.set_attribute("messaging.routing_key", routing_key)
        try:
            data = _encode_event(dict(payload))
        except Exception:
            logger.exception("payload 编码失败 rk=%s", routing_key)
            notify_failed_total.inc()
            return False
        # 事件契约校验
        problems = _contract_violations(json.loads(data), routing_key)
        if problems:
            _record_violation(payload.get("fn"), "produce", problems, topic=topic)
            return False
        props: dict[str, str] = {"routing_key": routing_key}
        fn = payload.get("fn")
        if isinstance(fn, str):
            props["fn"] = fn
        event_id = payload.get("event_id")
        if isinstance(event_id, str):
            props["event_id"] = event_id
        tracing.inject_context(props)

        transport = _transport
        if transport is not None:
            try:
                await transport.publish(topic, data, props)
                return True
            except Exception:
                logger.exception("transport publish failed rk=%s", routing_key)
                notify_failed_total.inc()
                return False

        if not settings.message_bus_enabled:
            return False
        try:
            if settings.message_bus == "rabbitmq":
                from core import rabbitmq

                await rabbitmq.publish(topic, data, props)
                return True
            producer = await _get_producer(topic)
            if _closed:
                return False
            await asyncio.to_thread(producer.send, dict(payload), properties=props)
            return True
        except Exception:
            logger.exception(
                "message publish failed rk=%s topic=%s", routing_key, topic
            )
            notify_failed_total.inc()
            return False


async def _release_resources() -> None:
    """
    释放 producer 缓存与 client，并复位 transport（不置终态标记）。
    """
    global _client, _transport
    from core import rabbitmq

    await rabbitmq.close()
    _transport = None
    with _client_lock:
        producers = list(_producers.values())
        _producers.clear()
        client = _client
        _client = None
    for producer in producers:
        with suppress(Exception):
            await asyncio.to_thread(producer.close)
    if client is not None:
        with suppress(Exception):
            await asyncio.to_thread(client.close)


async def close() -> None:
    """
    幂等收尾：关闭 producer 缓存与客户端（**可复位**，测试/重连场景用）。
    """
    await _release_resources()


async def shutdown() -> None:
    """
    终态关闭（应用 shutdown 专用）：释放资源后置「已关闭」标记。
    """
    global _closed
    _closed = True
    await _release_resources()
