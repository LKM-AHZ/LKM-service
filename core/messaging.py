"""
消息总线抽象：逻辑 routing_key → Pulsar topic，业务发布/消费无感。
- ``ROUTING_KEY_TOPICS``：逻辑 routing_key → ``persistent://{tenant}/{ns}/{name}`` 映射，
  业务侧只认 routing_key（send_code / notify_upload / apply_point / user.* / cron.*），
  不感知命名空间与 topic 名。
- ``SUBSCRIPTIONS``：订阅清单（订阅名 → topic + 关注 routing_key 列表）。订阅名同时是
  消费幂等 scope（见 ``app/db/event_processed.py``）与故障隔离单元。
- JSON Schema：每个 topic 挂同一 envelope JSON Schema，经 Pulsar 自带 schema registry 校验；
  envelope 为 ``{"fn": str, "args": list, "event_id"?: str}``，故用自定义 ``Schema`` 子类承载
  裸 JSON Schema（Pulsar ``JsonSchema`` 强制 avro ``Record`` dataclass，无法表达混合类型 args）。
发布/消费哲学沿用旧 ``amqp`` 层：**fail-open**——未配置 broker 或投递异常时不阻塞请求
（返回 False 由调用方降级），异常计数 ``notify_failed_total``。
Pulsar 官方 Python 客户端是**同步阻塞** API，故：
- 发布侧：producer 懒建缓存，``producer.send`` 经 ``asyncio.to_thread`` 执行。
- 消费侧：每个订阅一个 daemon 线程跑 ``consumer.receive``，消息经
  ``asyncio.run_coroutine_threadsafe`` 桥回主事件循环执行 async handler；成功 ack、异常负确认（触发 redelivery / 死信）。
测试 seam：``set_transport(InMemoryTransport())`` 注入内存替身，默认套件不依赖真实 broker。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import threading
from collections.abc import Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from core import tracing
from core.config import settings
from core.metrics import notify_failed_total

logger = logging.getLogger("lkm.messaging")

# 单任务执行上限（秒）：与迁移前 worker.JOB_TIMEOUT_S 对齐，超时视为消费失败 → 负确认。
JOB_TIMEOUT_S = 120

# 消费者建连/重建的退避间隔（秒）：空 topic 并发首订的 schema 注册竞态失败后重试用
_CONSUMER_RETRY_S = 2.0

# ---- routing_key 常量（业务唯一入口；原 core/jobs.py、core/worker.py 的 RKEY_* 迁此）----
RKEY_SEND_CODE = "event.send_code"
RKEY_SEND_MAGIC = "event.send_magic_link"
RKEY_NOTIFY = "event.notify_upload"
RKEY_POINTS = "event.apply_point"
RKEY_USER_UPDATED = "event.user.updated"
RKEY_USER_BANNED = "event.user.banned"
RKEY_USER_SESSION_REVOKE = "event.user.session_revoke"
RKEY_CLEANUP = "cron.cleanup"
RKEY_RECONCILE = "cron.reconcile"
RKEY_ANALYTICS = "cron.analytics_export"
RKEY_OPS_DAILY = "cron.ops_daily"
RKEY_BLOOM_SEED = "cron.bloom_seed"
# 内容域领域事件：外部检索引擎（Meilisearch/OpenSearch）增量同步的数据源
RKEY_CONTENT_PUBLISHED = "event.content.published"
RKEY_CONTENT_UPDATED = "event.content.updated"
RKEY_CONTENT_DELETED = "event.content.deleted"
RKEY_AUDIT_LOGIN_FAIL = "audit.login_fail"
RKEY_AUDIT_PERMISSION_CHANGE = "audit.permission_change"

# ---- topic 定案（tenant 取 settings.pulsar_tenant；namespace: auth / biz / system）----
NAMESPACES: frozenset[str] = frozenset({"auth", "biz", "system"})


def _topic(namespace: str, name: str) -> str:
    if namespace not in NAMESPACES:
        raise ValueError(
            f"unknown pulsar namespace {namespace!r}; allowed: {sorted(NAMESPACES)}"
        )
    return f"persistent://{settings.pulsar_tenant}/{namespace}/{name}"


def namespace_of(topic: str) -> str:
    """
    取 topic 全名的命名空间段（`persistent://<tenant>/<ns>/<name>`）。
    """
    parts = topic.split("/")
    # ["persistent:", "", tenant, namespace, ...] → 命名空间在 index 3
    if len(parts) < 5 or parts[0] != "persistent:" or parts[1] != "":
        raise ValueError(f"malformed pulsar topic: {topic!r}")
    return parts[3]


def _validate_namespace_isolation() -> None:
    """
    装配期校验命名空间隔离
    """
    for routing_key, topic in ROUTING_KEY_TOPICS.items():
        ns = namespace_of(topic)
        if ns not in NAMESPACES:
            raise ValueError(
                f"routing_key {routing_key!r} → 未知命名空间 {ns!r}: {topic}"
            )
    for sub in SUBSCRIPTIONS.values():
        sub_ns = namespace_of(sub.topic)
        if sub_ns not in NAMESPACES:
            raise ValueError(f"订阅 {sub.name!r} 使用未知命名空间 {sub_ns!r}")
        for routing_key in sub.routing_keys:
            topic = ROUTING_KEY_TOPICS.get(routing_key)
            if topic is None:
                raise ValueError(
                    f"订阅 {sub.name!r} 声明了未知 routing_key {routing_key!r}"
                )
            if namespace_of(topic) != sub_ns:
                raise ValueError(
                    f"订阅 {sub.name!r} 跨命名空间：{routing_key!r} → {topic}"
                    f"（订阅 topic 在 {sub_ns!r}）"
                )
            if topic != sub.topic:
                raise ValueError(
                    f"订阅 {sub.name!r} topic 不匹配：{routing_key!r} → {topic}"
                    f"（订阅 topic 为 {sub.topic}）"
                )


TOPIC_EMAIL = _topic("auth", "email")
TOPIC_USER_EVENTS = _topic("auth", "user.events")
TOPIC_AUDIT_LOGIN_FAIL = _topic("auth", "audit.login_fail")
TOPIC_AUDIT_PERMISSION_CHANGE = _topic("auth", "audit.permission_change")
TOPIC_NOTIFY = _topic("biz", "notify.upload")
TOPIC_POINTS = _topic("biz", "points.apply")
TOPIC_CONTENT = _topic("biz", "content.events")
TOPIC_CRON = _topic("system", "cron")
TOPIC_DLQ = _topic("system", "dlq")

# 逻辑 routing_key → topic（发布唯一查表口）
ROUTING_KEY_TOPICS: dict[str, str] = {
    RKEY_SEND_CODE: TOPIC_EMAIL,
    RKEY_SEND_MAGIC: TOPIC_EMAIL,
    RKEY_USER_UPDATED: TOPIC_USER_EVENTS,
    RKEY_USER_BANNED: TOPIC_USER_EVENTS,
    RKEY_USER_SESSION_REVOKE: TOPIC_USER_EVENTS,
    RKEY_AUDIT_LOGIN_FAIL: TOPIC_AUDIT_LOGIN_FAIL,
    RKEY_AUDIT_PERMISSION_CHANGE: TOPIC_AUDIT_PERMISSION_CHANGE,
    RKEY_NOTIFY: TOPIC_NOTIFY,
    RKEY_POINTS: TOPIC_POINTS,
    RKEY_CONTENT_PUBLISHED: TOPIC_CONTENT,
    RKEY_CONTENT_UPDATED: TOPIC_CONTENT,
    RKEY_CONTENT_DELETED: TOPIC_CONTENT,
    RKEY_CLEANUP: TOPIC_CRON,
    RKEY_RECONCILE: TOPIC_CRON,
    RKEY_ANALYTICS: TOPIC_CRON,
    RKEY_OPS_DAILY: TOPIC_CRON,
    RKEY_BLOOM_SEED: TOPIC_CRON,
}


@dataclass(frozen=True)
class Subscription:
    """
    一个 Pulsar 订阅（消费隔离单元）。
    ``name`` 同时是消费幂等 scope（多订阅消费同一 topic 时，各自独立记账，互不跳过）。
    ``routing_keys`` 为该订阅关注的逻辑事件集合（启动校验与文档用途）。
    """

    name: str
    topic: str
    routing_keys: tuple[str, ...] = ()


# 订阅清单：与部署 worker 进程一一对应（points 三订阅同 topic 实现扇出）。
SUB_SEND = Subscription("send", TOPIC_EMAIL, (RKEY_SEND_CODE, RKEY_SEND_MAGIC))
SUB_NOTIFY = Subscription("notify", TOPIC_NOTIFY, (RKEY_NOTIFY,))
SUB_POINTS_REWARD = Subscription("points-reward", TOPIC_POINTS, (RKEY_POINTS,))
SUB_POINTS_STATS = Subscription("points-stats", TOPIC_POINTS, (RKEY_POINTS,))
SUB_POINTS_TASKS = Subscription("points-tasks", TOPIC_POINTS, (RKEY_POINTS,))
# 站内信生成。与 points 三订阅同 topic 不同订阅名 → 各收全量、独立幂等 scope。
SUB_NOTIFICATION = Subscription("notification", TOPIC_POINTS, (RKEY_POINTS,))
SUB_USER_INVALIDATE = Subscription(
    "user-invalidate",
    TOPIC_USER_EVENTS,
    (RKEY_USER_UPDATED, RKEY_USER_BANNED, RKEY_USER_SESSION_REVOKE),
)
SUB_JOBS = Subscription(
    "jobs",
    TOPIC_CRON,
    (RKEY_CLEANUP, RKEY_RECONCILE, RKEY_ANALYTICS, RKEY_OPS_DAILY, RKEY_BLOOM_SEED),
)
SUB_CONTENT_INDEX = Subscription(
    "content-index",
    TOPIC_CONTENT,
    (RKEY_CONTENT_PUBLISHED, RKEY_CONTENT_UPDATED, RKEY_CONTENT_DELETED),
)
SUB_DLQ = Subscription("dlq-persist", TOPIC_DLQ)
SUB_AUDIT = Subscription("audit", TOPIC_AUDIT_LOGIN_FAIL, (RKEY_AUDIT_LOGIN_FAIL,))
SUB_AUDIT_PERMISSION = Subscription(
    "audit-permission",
    TOPIC_AUDIT_PERMISSION_CHANGE,
    (RKEY_AUDIT_PERMISSION_CHANGE,),
)

SUBSCRIPTIONS: dict[str, Subscription] = {
    s.name: s
    for s in (
        SUB_SEND,
        SUB_NOTIFY,
        SUB_POINTS_REWARD,
        SUB_POINTS_STATS,
        SUB_POINTS_TASKS,
        SUB_NOTIFICATION,
        SUB_USER_INVALIDATE,
        SUB_JOBS,
        SUB_CONTENT_INDEX,
        SUB_AUDIT,
        SUB_AUDIT_PERMISSION,
        SUB_DLQ,
    )
}

_validate_namespace_isolation()

EVENT_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "EventEnvelope",
    "namespace": "lkm.event",
    "fields": [
        {"name": "fn", "type": "string"},
        {
            "name": "args",
            "type": {
                "type": "array",
                "items": ["null", "boolean", "long", "double", "string"],
            },
            "default": [],
        },
        {"name": "event_id", "type": ["null", "string"], "default": None},
    ],
}

# 每个 topic 一份 schema（envelope 形态一致；集中定义便于审计 schema 演化）。
TOPIC_SCHEMAS: dict[str, dict[str, Any]] = {
    TOPIC_EMAIL: EVENT_SCHEMA,
    TOPIC_USER_EVENTS: EVENT_SCHEMA,
    TOPIC_AUDIT_LOGIN_FAIL: EVENT_SCHEMA,
    TOPIC_AUDIT_PERMISSION_CHANGE: EVENT_SCHEMA,
    TOPIC_NOTIFY: EVENT_SCHEMA,
    TOPIC_POINTS: EVENT_SCHEMA,
    TOPIC_CONTENT: EVENT_SCHEMA,
    TOPIC_CRON: EVENT_SCHEMA,
    TOPIC_DLQ: EVENT_SCHEMA,
}


def _encode_event(obj: Any) -> bytes:
    """事件 JSON 编码（发布与 schema.encode 共用，保证线上格式一致）。"""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


# ---- 自定义 JSON Schema（承载裸 JSON Schema 定义，走 Pulsar schema registry）----


def make_event_schema(topic: str) -> Any:
    """构造该 topic 的 Pulsar JSON Schema 实例（延迟 import，避免测试无 pulsar 时加载）。"""
    from pulsar.schema import Schema

    # _pulsar 为 C 扩展、无类型 stub，用动态 import 规避静态解析失败。
    schema_type = importlib.import_module("_pulsar").SchemaType.JSON
    definition = TOPIC_SCHEMAS.get(topic, EVENT_SCHEMA)

    class _EventJsonSchema(Schema):
        def __init__(self) -> None:
            super().__init__(dict, schema_type, definition, "EVENT_JSON")

        def encode(self, obj: Any) -> bytes:
            return _encode_event(obj)

        def decode(self, data: bytes) -> Any:
            return json.loads(data)

    return _EventJsonSchema()


def _contract_violations(payload: Any, routing_key: str | None) -> list[str]:
    """
    事件契约校验的延迟入口。
    ``event_contract`` 反向 import 本模块的 RKEY 常量（它按「逻辑键 → 允许的 fn」声明），
    模块级 import 会成环；放函数里只是 ``sys.modules`` 查表，成本可忽略。
    """
    from core import event_contract

    return event_contract.payload_violations(payload, routing_key)


def _record_violation(fn: Any, side: str, problems: list[str], *, topic: str) -> None:
    """记一次契约违约（延迟 import，理由同 :func:`_contract_violations`）。"""
    from core import event_contract

    event_contract.record_violation(fn, side, problems, where=f"topic={topic}")


def contract_violations(routing_key: str, payload: Any) -> list[str]:
    """
    按**线上形态**校验 envelope 的事件契约；返回违约原因清单（空=合规）。
    与 :func:`permanent_failure_reason` 共用同一判据。独立暴露是为了让 relay 在**折叠**这类
    事件时能拿到结构化原因去记违约指标，而不必去解析原因字符串。
    """
    try:
        wire = json.loads(_encode_event(dict(payload)))
    except (TypeError, ValueError):
        return []  # 编码失败是另一类永久失败，由 permanent_failure_reason 单独报
    return _contract_violations(wire, routing_key)


def permanent_failure_reason(
    routing_key: str, payload: Mapping[str, Any]
) -> str | None:
    """
    判定一次发布是否属**永久失败**（重试无意义）；返回原因字符串，None = 可重试。
    只覆盖 relay 侧在投递前就可确定的确定性错误：
    - 未知 ``routing_key``：不在 ``ROUTING_KEY_TOPICS`` 里，连目标 topic 都定不出来；
    - payload 无法按线上格式编码（含非 JSON 可序列化对象）；
    - **事件契约违约**（``event_contract.payload_violations``，见该模块头）：fn 未登记、
      实参个数/类型不符、经不允许的 routing_key 承载等。
    其余情况（连接失败 / 超时 / 总线不可达 / broker 拒收）一律**不**判永久
    """
    if routing_key not in ROUTING_KEY_TOPICS:
        return f"unknown routing_key={routing_key}"
    try:
        _encode_event(dict(payload))
    except (TypeError, ValueError) as exc:
        return f"payload not json-encodable: {exc}"
    problems = contract_violations(routing_key, payload)
    if problems:
        return "event contract violation: " + "; ".join(problems)
    return None


# ---- 发布 ----
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
        cached = _producers.setdefault(topic, producer)
    if cached is not producer:
        with suppress(Exception):
            producer.close()
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
        "pulsar.publish"
    ) as span:
        with suppress(Exception):
            span.set_attribute("messaging.system", "pulsar")
            span.set_attribute("messaging.destination.name", topic)
            span.set_attribute("messaging.pulsar.routing_key", routing_key)
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
            producer = await _get_producer(topic)
            await asyncio.to_thread(producer.send, dict(payload), properties=props)
            return True
        except Exception:
            logger.exception("pulsar publish failed rk=%s topic=%s", routing_key, topic)
            notify_failed_total.inc()
            return False


# ---- 消费（同步 client 的线程桥接）----


def _create_consumer_sync(sub: Subscription) -> Any:
    """建订阅消费者（Shared + 死信策略）——同步，须在线程中调用。"""
    import pulsar

    kwargs: dict[str, Any] = {}
    if sub.name != SUB_DLQ.name:
        kwargs["dead_letter_policy"] = pulsar.ConsumerDeadLetterPolicy(
            max_redeliver_count=settings.pulsar_dlq_max_redeliver,
            dead_letter_topic=TOPIC_DLQ,
        )
    return _get_client_sync().subscribe(
        sub.topic,
        sub.name,
        consumer_type=pulsar.ConsumerType.Shared,
        schema=make_event_schema(sub.topic),
        **kwargs,
    )


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
            logger.warning("非法消息转死信 subscription=%s bytes=%s", sub_name, len(raw))
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


async def _release_resources() -> None:
    """
    释放 producer 缓存与 client，并复位 transport（不置终态标记）。
    """
    global _client, _transport
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
