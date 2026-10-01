"""
Pulsar worker：注册表驱动的订阅消费。
每个 worker 进程常驻消费一个（或一组）Pulsar 订阅，按 payload.fn 从注册表分发 handler。
各模块 ``tasks.py`` 经 ``task_registry.register_task`` 把 handler 注册到订阅名下（订阅本身
定义在 ``core.messaging.SUBSCRIPTIONS``）；**新增任务不再改本文件**。
- 死信：Pulsar DeadLetterPolicy 在消费失败重投超限后投到 ``system/dlq`` topic，
  由 ``worker_dlq`` 消费落库（见 app/core/worker_dlq.py）。
- 幂等：``_dispatch_with_dedup`` 复用 ``EventProcessed`` 账本，``scope`` = 订阅名，
  多订阅消费同一事件各自独立记账（points 扇出）。
- cron：scheduler 发布 ``cron.*`` 消息到 ``system/cron`` topic，由 jobs 订阅消费。
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import text

from core import event_contract, messaging, metrics_relay, task_registry
from core.db.event_processed import DEFAULT_SCOPE, already_processed, record_processed
from core.db.session import new_worker_session as new_session
from core.logging import log_exceptions
from core.tracing import setup_tracing

logger = logging.getLogger("lkm.worker")

SEND_SUBSCRIPTION = messaging.SUB_SEND.name
NOTIFY_SUBSCRIPTION = messaging.SUB_NOTIFY.name
POINTS_REWARD_SUBSCRIPTION = messaging.SUB_POINTS_REWARD.name
POINTS_STATS_SUBSCRIPTION = messaging.SUB_POINTS_STATS.name
POINTS_TASKS_SUBSCRIPTION = messaging.SUB_POINTS_TASKS.name
NOTIFICATION_SUBSCRIPTION = messaging.SUB_NOTIFICATION.name
USER_INVALIDATE_SUBSCRIPTION = messaging.SUB_USER_INVALIDATE.name
JOBS_SUBSCRIPTION = messaging.SUB_JOBS.name
CONTENT_INDEX_SUBSCRIPTION = messaging.SUB_CONTENT_INDEX.name
AUDIT_SUBSCRIPTION = messaging.SUB_AUDIT.name
AUDIT_PERMISSION_SUBSCRIPTION = messaging.SUB_AUDIT_PERMISSION.name

# worker_dlq 消费的死信 topic。
DLQ = messaging.TOPIC_DLQ


async def _dispatch_with_dedup(
    payload: dict[str, Any],
    handler: Any,
    args: list[Any],
    *,
    scope: str = DEFAULT_SCOPE,
) -> None:
    """
    带幂等的任务分派（供消费回调复用）。
    - payload 带 event_id（outbox relay 发布透传）→ 开临时会话查 event_processed：
      已处理 → 返回（外层对其 ack，不二次执行）；未处理 → 跑 handler，成功后记账。
    - 无 event_id（send/cron 等直发）→ 原语义直跑，不经 DB，零额外开销。
    - ``scope`` = 订阅名：同一事件被多个订阅消费（points 扇出）时各订阅独立记账，互不误跳过。
    用事务级 advisory lock 串行同一 (scope,event_id) 的查账/handler/记账，避免并发双跑。
    handler 成功但进程在记账前崩溃时仍可能重跑，副作用须由 handler 自身幂等兜底。
    """
    eid = payload.get("event_id")
    if not isinstance(eid, str) or not eid:
        await handler(*args)
        return
    db = await new_session()
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{scope}:{eid}"},
        )
        if await already_processed(db, eid, scope=scope):
            logger.info("幂等跳过已处理 scope=%s event_id=%s", scope, eid)
            await db.rollback()
            return
        await handler(*args)
        await record_processed(db, eid, scope=scope)
    finally:
        if db.in_transaction():
            await db.rollback()
        await db.close()


async def _consume(subscription_name: str) -> None:
    """常驻消费一个 Pulsar 订阅，按 payload.fn 从注册表分发 handler。

    成功 → ack；handler 异常/超时 → core.messaging 负确认（重投超限后进死信）。
    """
    # 初始化 worker 的追踪和指标上报。
    setup_tracing(service_suffix="-worker")
    metrics_relay.start_publisher()

    handlers = task_registry.handlers_for(subscription_name)

    @log_exceptions
    async def _on_payload(payload: dict[str, Any], meta: messaging.MessageMeta) -> None:
        # 契约错误负确认；缺少 routing_key 时只校验 fn/args。
        problems = event_contract.payload_violations(
            payload, meta.properties.get("routing_key")
        )
        if problems:
            event_contract.record_violation(
                payload.get("fn"),
                "consume",
                problems,
                where=f"subscription={subscription_name}",
            )
            raise ValueError(f"invalid event contract: {problems}")
        handler = handlers.get(payload["fn"])
        if handler is None:
            logger.warning(
                "未知任务 %s, 转死信 subscription=%s", payload["fn"], subscription_name
            )
            raise ValueError(f"unknown task {payload['fn']}")
        await _dispatch_with_dedup(
            payload, handler, payload.get("args", []), scope=subscription_name
        )

    await messaging.run_subscription(subscription_name, _on_payload)




async def run_send_worker() -> None:
    await _consume(SEND_SUBSCRIPTION)


async def run_notify_worker() -> None:
    await _consume(NOTIFY_SUBSCRIPTION)


async def run_points_reward_worker() -> None:
    await _consume(POINTS_REWARD_SUBSCRIPTION)


async def run_points_stats_worker() -> None:
    await _consume(POINTS_STATS_SUBSCRIPTION)


async def run_points_tasks_worker() -> None:
    await _consume(POINTS_TASKS_SUBSCRIPTION)


async def run_notification_worker() -> None:
    await _consume(NOTIFICATION_SUBSCRIPTION)


async def run_content_index_worker() -> None:
    """content-index worker：消费 content.* 事件，增量同步外部检索索引。"""
    await _consume(CONTENT_INDEX_SUBSCRIPTION)


async def run_points_worker() -> None:
    """兼容旧入口：points 拆三订阅后默认跑 reward 订阅（生产用三个独立入口）。"""
    await _consume(POINTS_REWARD_SUBSCRIPTION)


async def run_default_worker() -> None:
    """jobs worker：并行消费 cron、auth 用户事件失效、以及 audit.* 审计订阅。

    audit.* 的消费体只是「记一个指标 + 一条结构化日志」（见 ``auth.tasks.record_audit_event``），
    不落库、无外部依赖，故与 user-invalidate 同款**折进本 worker**，不为它单开容器；
    单开会让部署面多一个空转进程，而收益只是隔离——这里没有需要隔离的重活。
    """
    await asyncio.gather(
        _consume(JOBS_SUBSCRIPTION),
        _consume(USER_INVALIDATE_SUBSCRIPTION),
        _consume(AUDIT_SUBSCRIPTION),
        _consume(AUDIT_PERMISSION_SUBSCRIPTION),
    )
