"""Prefect 定时触发入口：只发布事件，业务处理仍由 jobs worker 完成。"""

from prefect import flow

from core.scheduler import fire_cron_job


@flow(name="cron-dispatch")
async def cron_dispatch_flow(job_id: str) -> None:
    await fire_cron_job(job_id)
