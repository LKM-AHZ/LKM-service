"""APScheduler 独立调度进程：cron 到点发布 cron.* 消息到消息总线（system/cron topic）。

不直接执行任务——只把触发作为普通消息发布，由 jobs 订阅 worker 消费。
与消息系统解耦，消息总线不可用时发布 fail-open（日志+跳过），下次整点再触发。

cron 任务清单由各模块 ``tasks.py`` 经 ``task_registry.register_cron_job`` 声明，
本模块从注册表聚合构建调度器——**加 cron 任务不再改本文件**。

*fn* 必须与对应模块 tasks.py 注册的 handler 键（register_task 的 fn）精确一致——
否则 worker 按 fn 查表得 None 会当"未知任务"丢弃。注册表已免除两处的强耦合
（fn/routing_key 都在同一条 register_cron_job 声明里成对给出）。
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core import messaging, task_registry

logger = logging.getLogger("lkm.scheduler")


async def _fire(routing_key: str, fn: str) -> None:
    ok = await messaging.publish(routing_key, {"fn": fn})
    if not ok:
        logger.warning("cron %s 发布失败(fail-open), fn=%s", routing_key, fn)


def build_scheduler() -> AsyncIOScheduler:
    """从 task_registry 聚合全部 cron 任务，构建调度器。

    ``cron`` 为 crontab 表达式，经 CronTrigger.from_crontab 解析。
    """
    task_registry.ensure_tasks_registered()
    s = AsyncIOScheduler()
    for job in task_registry.cron_jobs():
        # 按 job 隔离：cron 表达式/字段来自各模块 tasks.py 的声明（外部输入），任一写错
        # （from_crontab 抛 ValueError 或 KeyError）都不该让调度进程起不来、把**全部** cron
        # 拖停——与文件头的 fail-open 理念一致；日志带 job id 便于定位是谁写错了
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
    return s
