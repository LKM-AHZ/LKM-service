"""feed 模块订阅任务：时间线写扩散（M6.11）。

``fanout_feed_items`` 每 2 分钟按源水位扫描新内容并写入关注者的物化 feed。
"""

import logging

from app.core.messaging import RKEY_RECONCILE, SUB_JOBS
from app.core.task_registry import register_cron_job, register_task
from app.db.session import new_worker_session as new_session

logger = logging.getLogger(__name__)


async def fanout_feed_items() -> None:
    """周期任务：把各源水位之后的新内容 fanout 给受众（无新内容时不做事）。"""
    from app.modules.feed.fanout import fanout_batch

    db = await new_session()
    try:
        processed = await fanout_batch(db)
        await db.commit()
        if processed:
            logger.info("feed fanout processed %d items", processed)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


register_task(SUB_JOBS.name, "fanout_feed_items", fanout_feed_items)
register_cron_job(
    job_id="fanout_feed_items",
    cron="*/2 * * * *",  # 每 2 分钟：物化 feed 的新鲜度上界
    routing_key=RKEY_RECONCILE,
    fn="fanout_feed_items",
)
