"""interaction 模块订阅任务：浏览记录保留策略清理（cron）。

``view_logs`` 是高频写表：行数上界虽是「用户数 × 内容数」，但历史内容多的站点仍会
随时间堆积。``purge_stale_view_logs`` 消费 cron.cleanup，按
``settings.interaction_view_log_retention_days`` 删除超期记录（默认 90 天）。
"""

import logging

from app.core.messaging import RKEY_CLEANUP, SUB_JOBS
from app.core.task_registry import register_cron_job, register_task
from app.db.session import new_session
from app.modules.interaction.service import purge_stale_view_logs as _purge

logger = logging.getLogger(__name__)


async def purge_stale_view_logs() -> None:
    """周期任务：删除超过保留期的浏览记录（无过期行时删除 0 行，不报错）。"""
    from app.core.config import settings

    db = await new_session()
    try:
        removed = await _purge(db, settings.interaction_view_log_retention_days)
        await db.commit()
        if removed:
            logger.info("purged %d stale view logs", removed)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


register_task(SUB_JOBS.name, "purge_stale_view_logs", purge_stale_view_logs)
register_cron_job(
    job_id="purge_stale_view_logs",
    cron="0 3 * * *",  # 每天 03:00（与整点上传清扫错峰）
    routing_key=RKEY_CLEANUP,
    fn="purge_stale_view_logs",
)
