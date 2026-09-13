"""队列任务注册表：模块声明任务归属，worker 按注册表通用消费（计划 §6.2）。

目标：**加任务不再改 worker.py**。每个业务模块在自己的 ``tasks.py``（或部署时被正确导入的文件）
里声明归属队列（``QUEUE``）与消费的 routing key（``ROUTING_KEYS``），并逐一 ``register_task``
注册 handler。worker 进程入口（worker_*.py → run_*_worker）只需触发各模块 tasks 导入
（副作用注册），再从本注册表读出该队列的 handler 表与拓扑声明即可。

保持四队列进程隔离（§8）：send / notify / points / jobs 仍是四个独立 worker 进程；
注册表只负责"哪个队列消费哪些任务键 / 声明哪些绑定"的单一事实源，不改变消费拓扑。

本模块不 import 任何业务模块（跨模块导入仅允许在模块的 ``tasks.py`` 声明侧）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger("lkm.task_registry")

# 单一事实源：queue_name -> 该队列绑定的 routing key 列表（用于拓扑声明）
_QUEUE_ROUTING: dict[str, list[str]] = {}

# M4 Pulsar：订阅名 -> 该订阅关注的 routing key 列表（worker 消费隔离单元，同 topic 可多订阅）
_SUBSCRIPTION_ROUTING: dict[str, list[str]] = {}
# 订阅名 -> Pulsar topic（发布侧由 core.messaging 映射表给出，此处留痕供校验/断言）
_SUBSCRIPTION_TOPICS: dict[str, str] = {}

# 单一事实源：queue_name / subscription_name -> {fn: handler}
_TASK_HANDLERS: dict[str, dict[str, Callable[..., Any]]] = {}

# 单一事实源：cron 任务声明（scheduler 聚合消费），供 APScheduler 构建
# CRON_JOBS: list[dict]，含 id/trigger(cron 名)/routing_key/fn
_CRON_JOBS: list[dict[str, Any]] = []


def register_cron_job(*, job_id: str, cron: str, routing_key: str, fn: str) -> None:
    """登记一条 cron 任务：到点由 scheduler 发布 ``fn`` 到 ``routing_key``。

    ``cron`` 为 APScheduler ``CronTrigger.from_crontab`` 可解析的 crontab 表达式
    （如 ``"0 * * * *"`` 每小时整点、``"0 4 * * 4"`` 每周四 04:00）。
    重复 job_id 会告警并覆盖。
    """
    for existing in _CRON_JOBS:
        if existing["id"] == job_id:
            logger.warning("cron job %r 重复登记，覆盖", job_id)
            existing.update(id=job_id, cron=cron, routing_key=routing_key, fn=fn)
            return
    _CRON_JOBS.append(
        {"id": job_id, "cron": cron, "routing_key": routing_key, "fn": fn}
    )


def cron_jobs() -> list[dict[str, Any]]:
    """当前全部已登记的 cron 任务（scheduler 聚合数据源）。"""
    return list(_CRON_JOBS)


def import_task_modules() -> None:
    """导入各模块 ``tasks.py`` 触发注册（副作用，幂等）。

    供 worker / scheduler / 单测在装配前调用，确保注册表被填满。任务逻辑内重型依赖
    均为函数级 import，此处仅触发注册，不拉业务整树。模块清单随新增业务域扩充。
    """
    import app.modules.auth.tasks
    import app.modules.blog.tasks
    import app.modules.files.tasks
    import app.modules.points.tasks  # noqa: F401


def ensure_tasks_registered() -> None:
    """确保已注册（guard 幂等，重复调用不重复触发）。"""
    if _QUEUE_ROUTING or _SUBSCRIPTION_ROUTING or _TASK_HANDLERS or _CRON_JOBS:
        return
    import_task_modules()


def register_subscription(name: str, topic: str, routing_keys: list[str]) -> None:
    """声明一个 Pulsar 订阅（消费隔离单元，M4）。幂等，重复声明取并集。

    ``name`` 同时是消费幂等 scope（见 app/db/event_processed.py）与发布侧对应订阅；
    ``topic`` 为该订阅消费的 Pulsar topic；同一 topic 可有多个订阅（points 三订阅扇出）。
    """
    keys = _SUBSCRIPTION_ROUTING.setdefault(name, [])
    for rk in routing_keys:
        if rk not in keys:
            keys.append(rk)
    _SUBSCRIPTION_TOPICS[name] = topic


def subscriptions() -> list[str]:
    """当前已声明的订阅名列表。"""
    return list(_SUBSCRIPTION_ROUTING)


def subscription_topic(name: str) -> str | None:
    """订阅对应的 Pulsar topic（未声明返回 None）。"""
    return _SUBSCRIPTION_TOPICS.get(name)


def subscription_topology() -> Mapping[str, list[str]]:
    """订阅名 → 关注 routing key 列表（幂等拓扑声明/断言的数据源）。"""
    return {name: list(keys) for name, keys in _SUBSCRIPTION_ROUTING.items()}


def register_queue(queue: str, routing_keys: list[str]) -> None:
    """声明队列及其消费的 routing key 集合（幂等，重复声明取并集）。

    ``routing_keys`` 决定拓扑声明（队列↔exchange 的绑定）。同一队列多次调用则合并。
    """
    existing = _QUEUE_ROUTING.setdefault(queue, [])
    for rk in routing_keys:
        if rk not in existing:
            existing.append(rk)


def register_task(queue: str, fn: str, handler: Callable[..., Any]) -> None:
    """注册某队列的单个任务 handler。重复注册同一 fn 会覆盖（以最后声明为准）并告警。"""
    table = _TASK_HANDLERS.setdefault(queue, {})
    if fn in table:
        logger.warning("task %r 重复注册于队列 %r，覆盖旧 handler", fn, queue)
    table[fn] = handler


def handlers_for(queue: str) -> dict[str, Callable[..., Any]]:
    """取出某队列的全部 handler 表（供通用消费循环按 payload.fn 分发）。"""
    return dict(_TASK_HANDLERS.get(queue, {}))


def topology() -> Mapping[str, list[str]]:
    """当前全部已注册队列 → routing key 列表（幂等拓扑声明的数据源）。"""
    return {q: list(rks) for q, rks in _QUEUE_ROUTING.items()}
