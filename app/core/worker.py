"""Pulsar worker：注册表驱动的订阅消费（M4）。

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

from app.core import messaging, task_registry
from app.core.tracing import setup_tracing
from app.db.event_processed import DEFAULT_SCOPE, already_processed, record_processed
from app.db.session import new_session

logger = logging.getLogger("lkm.worker")

# 单任务执行上限（秒）：与 core.messaging 的线程桥超时一致
JOB_TIMEOUT_S = messaging.JOB_TIMEOUT_S

# ---- 订阅名常量（部署编排与测试引用；与 core.messaging.SUBSCRIPTIONS 对齐）----
SEND_SUBSCRIPTION = messaging.SUB_SEND.name
NOTIFY_SUBSCRIPTION = messaging.SUB_NOTIFY.name
POINTS_REWARD_SUBSCRIPTION = messaging.SUB_POINTS_REWARD.name
POINTS_STATS_SUBSCRIPTION = messaging.SUB_POINTS_STATS.name
POINTS_TASKS_SUBSCRIPTION = messaging.SUB_POINTS_TASKS.name
NOTIFICATION_SUBSCRIPTION = messaging.SUB_NOTIFICATION.name
USER_INVALIDATE_SUBSCRIPTION = messaging.SUB_USER_INVALIDATE.name
JOBS_SUBSCRIPTION = messaging.SUB_JOBS.name

# 死信 topic（worker_dlq 消费）
DLQ = messaging.TOPIC_DLQ


def _ensure_models() -> None:
    """预注册全部 ORM 模型（防 worker 进程 SQLAlchemy mapper 缺失）。"""
    from app.db.model_registry import ensure_all_models

    ensure_all_models()


_ensure_models()
task_registry.import_task_modules()


async def _dispatch_with_dedup(
    payload: dict[str, Any],
    handler: Any,
    args: list[Any],
    *,
    scope: str = DEFAULT_SCOPE,
) -> None:
    """带幂等的任务分派（供消费回调复用）。

    - payload 带 event_id（outbox relay 发布透传）→ 开临时会话查 event_processed：
      已处理 → 返回（外层对其 ack，不二次执行）；未处理 → 跑 handler，成功后记账。
    - 无 event_id（send/cron 等直发）→ 原语义直跑，不经 DB，零额外开销。
    - ``scope`` = 订阅名：同一事件被多个订阅消费（points 扇出）时各订阅独立记账，互不误跳过。

    **去重强度是 best-effort，不是「恰好一次」**：查账（SELECT）→ 跑 handler → 记账
    （INSERT）三步非原子，故两个并发投递可能都判定「未处理」而各跑一遍；handler 跑成功但
    记账失败/进程被杀时账本无记录，重投也会再跑一遍。之所以不做「先原子占位再跑、失败回滚」
    的 claim-first：那会把风险从「重复执行」换成「硬崩溃后占位行残留 → 重投被静默跳过 →
    **事件丢失**」，与本仓 at-least-once + 消费端幂等（各 handler 自身幂等，且有 ref 等
    次级守约）的取向相反（重投可重、丢事件不可恢复）。
    """
    eid = payload.get("event_id")
    if not isinstance(eid, str) or not eid:
        await handler(*args)
        return
    db = await new_session()
    try:
        try:
            if await already_processed(db, eid, scope=scope):
                logger.info("幂等跳过已处理 scope=%s event_id=%s", scope, eid)
                return  # ack，不重复执行 handler
        except Exception:
            # 账本查不动：保守当作未记账，继续执行，避免一旦 DB 抖动业务停摆。
            logger.exception("event_processed 查账失败,继续执行 event_id=%s", eid)
        await handler(*args)
        try:
            await record_processed(db, eid, scope=scope)
        except Exception:
            # 记账失败(罕见)：已跑过一次副作用，宁可让 DLQ requeue 重试走幂等查账兜底。
            logger.exception(
                "event_processed 记账失败 scope=%s event_id=%s", scope, eid
            )
            raise
    finally:
        await db.close()


async def _consume(subscription_name: str) -> None:
    """常驻消费一个 Pulsar 订阅，按 payload.fn 从注册表分发 handler。

    成功 → ack；handler 异常/超时 → core.messaging 负确认（重投超限后进死信）。
    """
    # worker 进程不是 ASGI app：初始化 provider + httpx，让消费 span（含从消息属性
    # extract 出的上游 trace 上下文）能真正导出；不配 LKM_OTEL_ENABLED 时是 no-op。
    setup_tracing(service_suffix="-worker")

    handlers = task_registry.handlers_for(subscription_name)

    async def _on_payload(
        payload: dict[str, Any], _meta: messaging.MessageMeta
    ) -> None:
        fn = payload.get("fn")
        args = payload.get("args", [])
        if not isinstance(args, list):
            # args 形状非法（null/dict/str）：dict 会按 key 展开成多个实参、str 会按字符展开，
            # 都是「静默用错参数跑一遍」而非报错；与未知 fn 同样 ack 丢弃
            logger.warning(
                "args 类型非法(%s), 丢弃 subscription=%s fn=%s",
                type(args).__name__,
                subscription_name,
                fn,
            )
            return
        handler = handlers.get(fn) if isinstance(fn, str) else None
        if handler is None:
            logger.warning("未知任务 %s, 丢弃 subscription=%s", fn, subscription_name)
            return  # ack 丢弃
        await _dispatch_with_dedup(payload, handler, args, scope=subscription_name)

    await messaging.run_subscription(subscription_name, _on_payload)


# ---- worker 入口（各 worker_*.py 调用）----


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


async def run_points_worker() -> None:
    """兼容旧入口：points 拆三订阅后默认跑 reward 订阅（生产用三个独立入口）。"""
    await _consume(POINTS_REWARD_SUBSCRIPTION)


async def run_default_worker() -> None:
    """jobs worker：并行消费 cron 订阅与 auth 用户事件失效订阅。"""
    await asyncio.gather(
        _consume(JOBS_SUBSCRIPTION),
        _consume(USER_INVALIDATE_SUBSCRIPTION),
    )
