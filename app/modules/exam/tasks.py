"""exam 模块 cron：过期作答兜底扫描。

开考/交卷/查询路径已惰性过期；本任务每天补扫一次，兜住“用户开考后再也不回来”的
in_progress 行。只改状态，不评分、不发证书/积分，仍计入周期内已用次数。
"""

import logging

from app.modules.exam.service import expire_overdue_attempts
from core.db.session import new_worker_session as new_session
from core.messaging import RKEY_CLEANUP, SUB_JOBS
from core.task_registry import register_cron_job, register_task

logger = logging.getLogger(__name__)


async def expire_exam_attempts() -> None:
    db = await new_session()
    try:
        count = await expire_overdue_attempts(db)
        await db.commit()
        if count:
            logger.info("expired %d overdue exam attempts", count)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


register_task(SUB_JOBS.name, "expire_exam_attempts", expire_exam_attempts)
register_cron_job(
    job_id="expire_exam_attempts",
    cron="0 4 * * *",  # 每天 04:00；惰性过期仍覆盖即时场景
    routing_key=RKEY_CLEANUP,
    fn="expire_exam_attempts",
)
