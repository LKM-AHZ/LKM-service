"""
消息总线拓扑：逻辑 routing_key → topic 映射与订阅清单。

业务侧只认 routing_key（``send_code`` / ``notify_upload`` / ``apply_point`` / ``user.*`` /
``cron.*`` / ``content.*`` / ``file.*`` / ``audit.*``），不感知命名空间与 topic 名。
订阅名同时是消费幂等 scope（见 ``core/db/event_processed.py``）与故障隔离单元。

装配期 :func:`_validate_namespace_isolation` 兜底校验：任一 routing_key/订阅跨命名空间即
启动失败——命名空间（auth/biz/system）是 broker 侧的权限边界，静默越界等于绕过隔离。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import settings

# ---- 命名空间（broker 权限边界）----
NAMESPACES: frozenset[str] = frozenset({"auth", "biz", "system"})

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
RKEY_REVIEW_DIGEST = "cron.review_digest"
# 内容域领域事件：外部检索引擎（Meilisearch/OpenSearch）增量同步的数据源
RKEY_CONTENT_PUBLISHED = "event.content.published"
RKEY_CONTENT_UPDATED = "event.content.updated"
RKEY_CONTENT_DELETED = "event.content.deleted"
RKEY_FILE_CHANGED = "event.file.changed"
RKEY_AUDIT_LOGIN_FAIL = "audit.login_fail"
RKEY_AUDIT_PERMISSION_CHANGE = "audit.permission_change"


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


# ---- topic 定案（tenant 取 settings.pulsar_tenant；namespace: auth / biz / system）----
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
    RKEY_FILE_CHANGED: TOPIC_CONTENT,
    RKEY_CLEANUP: TOPIC_CRON,
    RKEY_RECONCILE: TOPIC_CRON,
    RKEY_ANALYTICS: TOPIC_CRON,
    RKEY_OPS_DAILY: TOPIC_CRON,
    RKEY_BLOOM_SEED: TOPIC_CRON,
    RKEY_REVIEW_DIGEST: TOPIC_CRON,
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
    (
        RKEY_CLEANUP,
        RKEY_RECONCILE,
        RKEY_ANALYTICS,
        RKEY_OPS_DAILY,
        RKEY_BLOOM_SEED,
        RKEY_REVIEW_DIGEST,
    ),
)
SUB_CONTENT_INDEX = Subscription(
    "content-index",
    TOPIC_CONTENT,
    (
        RKEY_CONTENT_PUBLISHED,
        RKEY_CONTENT_UPDATED,
        RKEY_CONTENT_DELETED,
        RKEY_FILE_CHANGED,
    ),
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


def make_topic(namespace: str, name: str) -> str:
    """构造 topic 全名（插件新增路由时用；命名空间必须在 :data:`NAMESPACES` 内）。"""
    return _topic(namespace, name)


def register_routing_key(routing_key: str, topic: str) -> None:
    """插件在导入期登记新的 routing_key → topic（同键同值幂等，冲突即报错）。"""
    if not isinstance(routing_key, str) or not routing_key:
        raise ValueError("routing_key 必须是非空字符串")
    existing = ROUTING_KEY_TOPICS.get(routing_key)
    if existing is not None:
        if existing == topic:
            return
        raise ValueError(
            f"routing_key {routing_key!r} 已指向 {existing!r}，不能再登记 {topic!r}"
        )
    ns = namespace_of(topic)
    if ns not in NAMESPACES:
        raise ValueError(f"routing_key {routing_key!r} 使用未知命名空间 {ns!r}")
    ROUTING_KEY_TOPICS[routing_key] = topic


def register_subscription(subscription: Subscription) -> None:
    """插件在导入期登记新订阅（消费进程仍需把该订阅挂到某个 worker 入口）。

    与 :data:`SUBSCRIPTIONS` 的表相同：订阅名唯一，同名同定义幂等、同名异定义报错。
    命名空间隔离与 routing_key 归属按内置订阅同一套规则即时校验。
    """
    if not isinstance(subscription.name, str) or not subscription.name:
        raise ValueError("订阅 name 必须是非空字符串")
    existing = SUBSCRIPTIONS.get(subscription.name)
    if existing is not None:
        if existing == subscription:
            return
        raise ValueError(
            f"订阅 {subscription.name!r} 已登记且定义不同："
            f"{existing!r} vs {subscription!r}"
        )
    _validate_subscription(subscription)
    SUBSCRIPTIONS[subscription.name] = subscription


def _validate_subscription(sub: Subscription) -> None:
    sub_ns = namespace_of(sub.topic)
    if sub_ns not in NAMESPACES:
        raise ValueError(f"订阅 {sub.name!r} 使用未知命名空间 {sub_ns!r}")
    for routing_key in sub.routing_keys:
        topic = ROUTING_KEY_TOPICS.get(routing_key)
        if topic is None:
            raise ValueError(f"订阅 {sub.name!r} 声明了未知 routing_key {routing_key!r}")
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


def _validate_namespace_isolation() -> None:
    """装配期校验命名空间隔离（插件随后新增的条目由注册函数即时校验）。"""
    for routing_key, topic in ROUTING_KEY_TOPICS.items():
        ns = namespace_of(topic)
        if ns not in NAMESPACES:
            raise ValueError(
                f"routing_key {routing_key!r} → 未知命名空间 {ns!r}: {topic}"
            )
    for sub in SUBSCRIPTIONS.values():
        _validate_subscription(sub)


_validate_namespace_isolation()
