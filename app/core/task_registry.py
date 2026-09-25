"""任务注册表：模块声明 handler 归属订阅，worker 按注册表通用分派（计划 §6.2）。

目标：**加任务不再改 worker.py**。每个业务模块在自己的 ``tasks.py`` 里用 ``register_task``
把 handler 注册到某个 Pulsar 订阅名下；worker 进程入口（worker_*.py → run_*_worker）触发
各模块 tasks 导入（副作用注册），再从本注册表读出该订阅的 handler 表即可。

订阅本身（订阅名 → topic / 关注的 routing_key）的单一事实源是
``core.messaging.SUBSCRIPTIONS``；本模块只负责"哪个订阅消费哪些 fn"，不重复存 topic/routing，
避免两处声明漂移。

本模块不 import 任何业务模块（跨模块导入仅允许在模块的 ``tasks.py`` 声明侧）；
auth 已独立成顶层包，其任务经 ``auth.register_tasks()`` 公开钩子注册，同样不直触内部。
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

# 全量导入是否已执行（显式标志，**不能用「注册表非空」代替**）：
# 任一模块的 ``tasks.py`` 被单独导入就会填 ``_TASK_HANDLERS``（如仅导入
# ``notification.tasks`` 只注册 handler、无 cron），若据此判定「已注册」就会跳过全量导入，
# 使 cron 声明与其余模块 handler 永久缺失（scheduler 拿到 0 个 job，2026-09-18 定位）。
_tasks_imported = False


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
    """当前全部已登记的 cron 任务（scheduler 聚合数据源）。

    逐条返回**副本**（同 :func:`handlers_for` 的拷贝语义）：调用方若就地对 job 做归一化/
    加字段（如把 cron 表达式换成 Trigger 后写回 dict），改到的是注册表的单一事实源，
    后续 build_scheduler 会拿到被污染的声明。
    """
    return [dict(job) for job in _CRON_JOBS]


def import_task_modules() -> None:
    """导入各模块 ``tasks.py`` 触发注册（副作用，幂等）。

    供 worker / scheduler / 单测在装配前调用，确保注册表被填满。任务逻辑内重型依赖
    均为函数级 import，此处仅触发注册，不拉业务整树。模块清单随新增业务域扩充。

    置 ``_tasks_imported``：已导入的模块被 ``sys.modules`` 缓存，重复调用不会再执行注册
    代码，故「是否跑过」只能由本标志承载（见 ``_tasks_imported`` 说明）。
    """
    global _tasks_imported

    # auth 已独立成顶层包：经其公开注册钩子触发（内部 import auth.tasks），
    # 本模块不直接触达 auth 内部模块。
    from auth import register_tasks

    register_tasks()
    import app.modules.content.blog.tasks
    import app.modules.content.tasks
    import app.modules.feed.tasks
    import app.modules.files.tasks
    import app.modules.interaction.tasks
    import app.modules.notification.tasks
    import app.modules.points.tasks
    import app.modules.search.tasks  # noqa: F401

    _tasks_imported = True


def ensure_tasks_registered() -> None:
    """确保已注册（guard 幂等，重复调用不重复触发）。"""
    if _tasks_imported:
        return
    import_task_modules()


def register_task(subscription: str, fn: str, handler: Callable[..., Any]) -> None:
    """注册某订阅的单个任务 handler。重复注册同一 fn 会覆盖（以最后声明为准）并告警。"""
    table = _TASK_HANDLERS.setdefault(subscription, {})
    if fn in table:
        logger.warning("task %r 重复注册于订阅 %r，覆盖旧 handler", fn, subscription)
    table[fn] = handler


def handlers_for(subscription: str) -> dict[str, Callable[..., Any]]:
    """取出某订阅的全部 handler 表（供消费循环按 payload.fn 分发）。"""
    return dict(_TASK_HANDLERS.get(subscription, {}))
