"""admin 域定时任务：每日待办摘要（站内信）。

与其它 cron 同走 ``jobs`` 订阅；scheduler 每天 12:00 发布 ``send_review_digest``。
任务本身只做一件事：调用 ``review_digest.send_daily_digest``（无实参，符合 cron 契约）。
"""

from __future__ import annotations

import logging

from core.messaging import RKEY_REVIEW_DIGEST, SUB_JOBS
from core.task_registry import register_cron_job, register_task

logger = logging.getLogger(__name__)


async def send_review_digest() -> None:
    """cron 入口：给管理员发每日待办摘要；失败向上抛由 jobs worker 记失败。"""
    from app.modules.admin.review_digest import send_daily_digest

    sent = await send_daily_digest()
    logger.info("send_review_digest done: %d", sent)


register_task(SUB_JOBS.name, "send_review_digest", send_review_digest)
register_cron_job(
    job_id="send_review_digest",
    cron="0 12 * * *",  # 每天 12:00（scheduler 进程时区）
    routing_key=RKEY_REVIEW_DIGEST,
    fn="send_review_digest",
)
