"""
APScheduler 独立调度进程：cron 到点发布 cron.* 消息到消息总线（system/cron topic）。
不直接执行任务——只把触发作为普通消息发布，由 jobs 订阅 worker 消费。
与消息系统解耦，发布失败会短暂重试三次；仍失败则记录错误，下次按 cron 再触发。
同一次触发的重试共用 event_id，供 jobs worker 在收到重复消息时去重。
cron 任务清单由各模块 ``tasks.py`` 经 ``task_registry.register_cron_job`` 声明，
本模块从注册表聚合构建调度器——**加 cron 任务不再改本文件**。
*fn* 必须与对应模块 tasks.py 注册的 handler 键（register_task 的 fn）精确一致——
否则 worker 按 fn 查表得 None 会当"未知任务"丢弃。注册表已免除两处的强耦合
（fn/routing_key 都在同一条 register_cron_job 声明里成对给出）。
"""

import asyncio
import logging
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core import messaging, scheduler_state, task_registry
from core.logging import log_exceptions

logger = logging.getLogger("lkm.scheduler")

_PUBLISH_ATTEMPTS = 3
_RETRY_DELAY_S = 1.0


@log_exceptions
async def _fire(routing_key: str, fn: str) -> None:
    # 在途作业数：进出各更新一次，收尾时可据此判断「未停残余」。
    # 只记进程内状态——对外暴露经 Redis 心跳由 API 进程的 reporter 完成（见 scheduler_state）。
    scheduler_state.note_job_started()
    try:
        # 同一次触发的重试共用 event_id：broker 已收下消息但客户端收到超时后，
        # 再次发布也能由 jobs worker 的账本去重。
        payload = {"fn": fn, "event_id": str(uuid4())}
        for attempt in range(1, _PUBLISH_ATTEMPTS + 1):
            try:
                if await messaging.publish(routing_key, payload):
                    return
            except Exception:
                logger.exception(
                    "cron %s 发布异常, fn=%s, attempt=%d", routing_key, fn, attempt
                )
            if attempt < _PUBLISH_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_S * attempt)
        logger.error(
            "cron %s 连续 %d 次发布失败, fn=%s",
            routing_key,
            _PUBLISH_ATTEMPTS,
            fn,
        )
    finally:
        scheduler_state.note_job_finished()


def build_scheduler() -> AsyncIOScheduler:
    """
    从 task_registry 聚合全部 cron 任务，构建调度器。
    ``cron`` 为 crontab 表达式，经 CronTrigger.from_crontab 解析。
    """
    task_registry.ensure_tasks_registered()
    s = AsyncIOScheduler()
    for job in task_registry.cron_jobs():
        # 按 job 隔离：cron 表达式/字段来自各模块 tasks.py 的声明（外部输入）
        try:
            trigger = CronTrigger.from_crontab(job["cron"])
            s.add_job(
                _fire,
                trigger,
                kwargs={"routing_key": job["routing_key"], "fn": job["fn"]},
                id=job["id"],
            )
        except (KeyError, ValueError):
            logger.exception("cron job %r 定义非法，跳过", job.get("id"))
            continue
    # 运行态的对外暴露在 worker_scheduler（起停点）与 _fire（在途数）里记，见 scheduler_state
    return s
