"""content 模块订阅任务：互动计数落库与对账（M6.10；B3 起 write-behind 退居回退）。

- ``flush_content_counters``（每分钟，**仅回退模式下注册**）：把 Redis 里的计数差值落进
  ``content_items``。写穿模式（默认）下写路径已直接落库，本 cron 无事可做故不注册。
- ``reconcile_content_counts``（每 15 分钟）：按明细表 ``COUNT(*)`` 重算并修正偏差，
  两种模式下都保留（历史脏值/异常路径的兜底收敛）。

对账与写路径的关系（关键，勿改成「reconcile 叠加 pending」）：对账**只以明细表为准**，
不叠加 Redis 里未落库的差值。这样任何时刻的重复/遗漏都会被下一轮对账拉回真值——若对账把
pending 也加进去，则「明细已提交 + pending 未 flush」会双计。
"""

import logging

from app.core.config import settings
from app.core.messaging import RKEY_CLEANUP, RKEY_RECONCILE, SUB_JOBS
from app.core.task_registry import register_cron_job, register_task
from app.db.session import new_session

logger = logging.getLogger(__name__)


async def flush_content_counters() -> None:
    """周期任务：Redis 计数差值 → DB 计数列（无待落库差值时不做事）。"""
    from app.modules.content.counters import flush_counters

    db = await new_session()
    try:
        applied = await flush_counters(db)
        await db.commit()
        if applied:
            logger.info("flushed content counters: %d rows", applied)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def reconcile_content_counts() -> None:
    """周期任务：按明细重算三项计数，修正偏差（收敛可证伪：二次 affected=0）。"""
    from app.modules.content.counters import reconcile_counts

    db = await new_session()
    try:
        scanned, affected = await reconcile_counts(db)
        await db.commit()
        logger.info(
            "reconciled content counts: scanned=%d affected=%d", scanned, affected
        )
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


register_task(SUB_JOBS.name, "flush_content_counters", flush_content_counters)
register_task(SUB_JOBS.name, "reconcile_content_counts", reconcile_content_counts)

# 写穿模式下写路径已直接落库、Redis 里不再有增量，flush cron 无事可做 → 不注册（少一个
# 每分钟的定时唤醒）。开关关闭（回退 write-behind）时才注册，见 core/config.py 的口径。
if not settings.counters_write_through:
    register_cron_job(
        job_id="flush_content_counters",
        cron="* * * * *",  # 每分钟：把增量落库（写路径不写计数列）
        routing_key=RKEY_CLEANUP,
        fn="flush_content_counters",
    )
register_cron_job(
    job_id="reconcile_content_counts",
    cron="*/15 * * * *",  # 每 15 分钟：以明细为真相源收敛
    routing_key=RKEY_RECONCILE,
    fn="reconcile_content_counts",
)
