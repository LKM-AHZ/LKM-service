"""
任务注册表：模块声明 handler 归属订阅，worker 按注册表通用分派。
目标：**加任务不再改 worker.py**。每个业务模块在自己的 ``tasks.py`` 里用 ``register_task``
把 handler 注册到某个 Pulsar 订阅名下；worker 进程入口（worker_*.py → run_*_worker）触发
各模块 tasks 导入（副作用注册），再从本注册表读出该订阅的 handler 表即可。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("lkm.task_registry")

# 单一事实源：subscription_name -> {fn: handler}
_TASK_HANDLERS: dict[str, dict[str, Callable[..., Any]]] = {}

# 单一事实源：cron 任务声明（scheduler 聚合消费），供 APScheduler 构建
# CRON_JOBS: list[dict]，含 id/trigger(cron 名)/routing_key/fn
_CRON_JOBS: list[dict[str, Any]] = []

_tasks_imported = False


def register_cron_job(*, job_id: str, cron: str, routing_key: str, fn: str) -> None:
    """
    登记一条 cron 任务：到点由 scheduler 发布 ``fn`` 到 ``routing_key``。
    ``cron`` 为 APScheduler ``CronTrigger.from_crontab`` 可解析的 crontab 表达式
    """
    from core import event_contract

    contract = event_contract.EVENT_CONTRACTS.get(fn)
    if contract is None:
        raise ValueError(
            f"cron 任务 {job_id!r} 的 fn={fn!r} 未登记事件契约"
            "（core/event_contract.EVENT_CONTRACTS）"
        )
    if contract.args:
        raise ValueError(
            f"cron 任务 {job_id!r} 的 fn={fn!r} 带实参契约，但 scheduler 只发 "
            '{"fn": fn}（core/scheduler.py）——二者不可能相容'
        )
    if routing_key not in contract.routing_keys:
        raise ValueError(
            f"cron 任务 {job_id!r} 经 {routing_key!r} 发布，但 fn={fn!r} 的契约只允许 "
            f"{list(contract.routing_keys)}"
        )

    for existing in _CRON_JOBS:
        if existing["id"] == job_id:
            logger.warning("cron job %r 重复登记，覆盖", job_id)
            existing.update(id=job_id, cron=cron, routing_key=routing_key, fn=fn)
            return
    _CRON_JOBS.append(
        {"id": job_id, "cron": cron, "routing_key": routing_key, "fn": fn}
    )


def cron_jobs() -> list[dict[str, Any]]:
    """
    当前全部已登记的 cron 任务（scheduler 聚合数据源）。
    逐条返回**副本**（同 :func:`handlers_for` 的拷贝语义）：调用方若就地对 job 做归一化/
    加字段（如把 cron 表达式换成 Trigger 后写回 dict），改到的是注册表的单一事实源，
    后续 build_scheduler 会拿到被污染的声明。
    """
    return [dict(job) for job in _CRON_JOBS]


_TASK_MODULES: list[str] = []


def register_module(path: str) -> None:
    """登记一个需在装配期导入的 ``tasks`` 模块（幂等；重复登记只保留一份）。"""
    if path not in _TASK_MODULES:
        _TASK_MODULES.append(path)


def import_task_modules() -> None:
    """
    导入全部已登记的 ``tasks`` 模块触发注册（副作用，幂等）。
    供装配根（``boot.assemble``）、worker / scheduler / 单测在启动前调用，确保注册表被填满。
    任务逻辑内重型依赖均为函数级 import，此处仅触发注册，不拉业务整树。
    """
    global _tasks_imported

    import importlib

    for path in _TASK_MODULES:
        importlib.import_module(path)

    _tasks_imported = True


def ensure_tasks_registered() -> None:
    """确保已注册（guard 幂等，重复调用不重复触发）。"""
    if _tasks_imported:
        return
    import_task_modules()


def _assert_subscription_carries(subscription: str, fn: str) -> None:
    """
    装配期校验：该订阅确实能收到这个 fn（否则 handler 永不触发，且无人报错）。
    判据 = 「订阅声明的 routing_keys」∩「契约允许承载该 fn 的 routing_key」非空。两侧的事实源
    分别是 ``messaging.SUBSCRIPTIONS`` 与 ``event_contract.EVENT_CONTRACTS``，本函数只做交叉。
    """
    from core import event_contract, messaging

    sub = messaging.SUBSCRIPTIONS.get(subscription)
    if sub is None:
        raise ValueError(f"订阅 {subscription!r} 未在 messaging.SUBSCRIPTIONS 登记")
    allowed = set(event_contract.EVENT_CONTRACTS[fn].routing_keys)
    if not allowed & set(sub.routing_keys):
        raise ValueError(
            f"订阅 {subscription!r}（routing_keys={list(sub.routing_keys)}）承载不了 "
            f"fn={fn!r}（契约允许 {sorted(allowed)}）——handler 将永不触发"
        )


def register_task(subscription: str, fn: str, handler: Callable[..., Any]) -> None:
    """
    注册某订阅的单个任务 handler。重复注册同一 fn 会覆盖（以最后声明为准）并告警。
    """
    from core import event_contract

    violation = event_contract.handler_violation(fn, handler)
    if violation is not None:
        raise ValueError(violation)
    _assert_subscription_carries(subscription, fn)

    table = _TASK_HANDLERS.setdefault(subscription, {})
    if fn in table:
        logger.warning("task %r 重复注册于订阅 %r，覆盖旧 handler", fn, subscription)
    table[fn] = handler


def handlers_for(subscription: str) -> dict[str, Callable[..., Any]]:
    """取出某订阅的全部 handler 表（供消费循环按 payload.fn 分发）。"""
    return dict(_TASK_HANDLERS.get(subscription, {}))
