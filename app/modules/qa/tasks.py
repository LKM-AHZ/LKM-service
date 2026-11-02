"""问答（QA）域订阅任务：到期悬赏结算。

步骤 9 切片 1 自 ``content.tasks`` 迁出（任务名 ``expire_qa_bounties`` 与调度表达式
不变，``core.event_contract`` 的契约登记无需改动）。
"""

from __future__ import annotations

from core.db.session import new_worker_session as new_session
from core.messaging import RKEY_CLEANUP, SUB_JOBS
from core.task_registry import register_cron_job, register_task


async def expire_qa_bounties() -> None:
    """小时级结算到期悬赏，失败时事务回滚供下一轮重试。"""
    from app.modules.qa.service import expire_due_questions

    while True:
        db = await new_session()
        try:
            count = await expire_due_questions(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        if count < 100:
            break


register_task(SUB_JOBS.name, "expire_qa_bounties", expire_qa_bounties)

register_cron_job(
    job_id="expire_qa_bounties",
    cron="0 * * * *",
    routing_key=RKEY_CLEANUP,
    fn="expire_qa_bounties",
)
