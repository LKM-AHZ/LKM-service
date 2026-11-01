"""
消费路径：把同步阻塞的 Pulsar ``consumer.receive`` 桥到主事件循环。

每个订阅一个 daemon 线程跑 ``receive``，消息经 ``asyncio.run_coroutine_threadsafe``
桥回主事件循环执行 async handler；成功 ack、异常负确认（触发 redelivery / 死信）。

``_create_consumer_sync`` / ``_handle_message`` / ``_CONSUMER_RETRY_S`` 是测试的注入点，
保持模块级定义以便 monkeypatch；对 runtime 持有的连接状态一律**经模块属性**访问
（``runtime._client`` 等），否则 patch 不会生效。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from contextlib import suppress
from typing import Any

from core import tracing
from core.bus import runtime
from core.bus.runtime import JOB_TIMEOUT_S, RAW_MESSAGE_KEY, MessageHandler, MessageMeta
from core.bus.topics import SUB_DLQ, SUBSCRIPTIONS, TOPIC_DLQ, Subscription
from core.config import settings

logger = logging.getLogger("lkm.messaging")

# 消费者建连/重建的退避间隔（秒）：空 topic 并发首订的 schema 注册竞态失败后重试用
_CONSUMER_RETRY_S = 2.0


def _create_consumer_sync(sub: Subscription) -> Any:
    """建订阅消费者（Shared + 死信策略）——同步，须在线程中调用。"""
    import pulsar

    kwargs: dict[str, Any] = {}
    if sub.name != SUB_DLQ.name:
        kwargs["dead_letter_policy"] = pulsar.ConsumerDeadLetterPolicy(
            max_redeliver_count=settings.pulsar_dlq_max_redeliver,
            dead_letter_topic=TOPIC_DLQ,
        )
    client = runtime._get_client_sync()
    consumer = client.subscribe(
        sub.topic,
        sub.name,
        consumer_type=pulsar.ConsumerType.Shared,
        schema=runtime.make_event_schema(sub.topic),
        **kwargs,
    )
    with runtime._client_lock:
        stale = runtime._closed or runtime._client is not client
    if stale:
        with suppress(Exception):
            consumer.close()
        raise RuntimeError("message bus closed while creating consumer")
    return consumer


async def _run_handler(
    handler: MessageHandler, payload: dict[str, Any], meta: MessageMeta
) -> None:
    """在主循环内带消费 span 执行 handler（从 meta.properties 续父链，M5 7.2.2）。"""
    with tracing.consume_span(meta.properties, meta.topic, meta.subscription):
        await handler(payload, meta)


def _handle_message(
    consumer: Any,
    msg: Any,
    handler: MessageHandler,
    loop: asyncio.AbstractEventLoop,
    sub_name: str,
) -> None:
    """
    处理一条消息：解析 → 桥回主循环执行 async handler → ack / negative_ack。
    非法 envelope 在源订阅负确认并最终进入死信；DLQ 将原始字节包装为 base64 交给
    持久化 handler。handler 异常/超时也负确认。注意超时后协程可能仍在执行，副作用靠
    handler 自身幂等兜底。
    """
    try:
        raw = msg.data()
    except Exception:
        logger.exception("读取消息失败 subscription=%s", sub_name)
        with suppress(Exception):
            consumer.negative_acknowledge(msg)
        return
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload 非 JSON 对象")
    except (TypeError, ValueError):
        if sub_name != SUB_DLQ.name:
            logger.warning(
                "非法消息转死信 subscription=%s bytes=%s", sub_name, len(raw)
            )
            with suppress(Exception):
                consumer.negative_acknowledge(msg)
            return
        logger.warning("死信消息无法解码，保存原始字节 bytes=%s", len(raw))
        payload = {RAW_MESSAGE_KEY: base64.b64encode(raw).decode("ascii")}

    try:
        properties = dict(msg.properties() or {})
    except Exception:
        properties = {}
    try:
        redelivery_count = int(msg.redelivery_count())
    except Exception:
        redelivery_count = 0
    try:
        source_message_id = base64.b64encode(msg.message_id().serialize()).decode(
            "ascii"
        )
    except Exception:
        source_message_id = None
    meta = MessageMeta(
        topic=SUBSCRIPTIONS[sub_name].topic,
        subscription=sub_name,
        properties=properties,
        redelivery_count=redelivery_count,
        message_id=source_message_id,
    )

    future = None
    try:
        future = asyncio.run_coroutine_threadsafe(
            _run_handler(handler, payload, meta), loop
        )
        future.result(timeout=JOB_TIMEOUT_S)
    except Exception:
        if future is not None:
            future.cancel()
        logger.exception("消费失败→负确认 subscription=%s", sub_name)
        with suppress(Exception):
            consumer.negative_acknowledge(msg)
        return

    with suppress(Exception):
        consumer.acknowledge(msg)


def _receive_loop(
    sub: Subscription,
    handler: MessageHandler,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    """
    订阅专用 daemon 线程主循环：建消费者 → receive(timeout) → 处理，收到 stop 后退出。
    非超时的 receive 异常会关闭坏连接并退避重建消费者。
    """
    import pulsar  # 局部导入：与文件其余处一致，无总线时不硬依赖客户端

    while not stop.is_set():
        try:
            consumer = _create_consumer_sync(sub)
        except Exception:
            logger.exception(
                "pulsar consumer 创建失败 subscription=%s; 退避后重试", sub.name
            )
            if stop.wait(_CONSUMER_RETRY_S):
                return
            continue
        logger.info("pulsar 订阅启动 subscription=%s topic=%s", sub.name, sub.topic)
        try:
            while not stop.is_set():
                try:
                    msg = consumer.receive(timeout_millis=1000)
                except pulsar.Timeout:
                    continue
                except Exception:
                    if stop.is_set():
                        break
                    logger.exception(
                        "pulsar receive 异常 subscription=%s; 关闭消费者后重建",
                        sub.name,
                    )
                    break
                _handle_message(consumer, msg, handler, loop, sub.name)
        finally:
            with suppress(Exception):
                consumer.close()
            logger.info("pulsar 订阅已停止 subscription=%s", sub.name)
        if stop.wait(_CONSUMER_RETRY_S):
            return


async def run_subscription(
    name: str,
    handler: MessageHandler,
) -> None:
    """
    常驻消费一个订阅，直到任务被取消。未配置消息总线 → 记录并空转退出。
    """
    if not settings.message_bus_enabled:
        # 只按总线配置与否判：注入的 transport 只覆盖 publish，不提供消费能力，
        # 总线未配置时起 daemon 线程只会用空 pulsar_url 反复建连失败
        logger.error("消息总线未配置，订阅 %s 无法启动", name)
        return
    sub = SUBSCRIPTIONS.get(name)
    if sub is None:
        logger.error("未知订阅名 %s", name)
        return
    if settings.message_bus == "rabbitmq":
        from core.bus.transports import rabbitmq

        await rabbitmq.run_subscription(sub, handler)
        return
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    thread = threading.Thread(
        target=_receive_loop,
        args=(sub, handler, loop, stop),
        name=f"pulsar-{name}",
        daemon=True,
    )
    thread.start()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    finally:
        stop.set()
        await asyncio.to_thread(thread.join, 5.0)
