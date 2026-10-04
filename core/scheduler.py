"""Prefect cron flow 的消息发布体；业务任务仍由 jobs worker 消费。"""

import asyncio
import logging
from uuid import uuid4

from core import messaging, task_registry
from core.logging import log_exceptions

logger = logging.getLogger("lkm.scheduler")

_PUBLISH_ATTEMPTS = 3
_RETRY_DELAY_S = 1.0


@log_exceptions
async def _fire(routing_key: str, fn: str) -> None:
    # 同一次触发的重试共用 event_id，worker 可去重。
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
    raise RuntimeError(f"cron {routing_key} 发布失败, fn={fn}")


async def fire_cron_job(job_id: str) -> None:
    """按注册表中的任务 ID 发布，拒绝未知 ID。"""
    task_registry.ensure_tasks_registered()
    for job in task_registry.cron_jobs():
        if job["id"] == job_id:
            if not job["enabled"]:
                raise ValueError(f"cron job 已停用: {job_id}")
            await _fire(job["routing_key"], job["fn"])
            return
    raise ValueError(f"未知 cron job: {job_id}")
