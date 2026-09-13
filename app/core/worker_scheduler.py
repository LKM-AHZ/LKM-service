"""compose worker-scheduler 服务入口：跑 APScheduler（cron 触发投递到消息总线）。"""

import asyncio
import logging

from app.core.scheduler import build_scheduler

logger = logging.getLogger("lkm.scheduler")


async def _main() -> None:
    sched = build_scheduler()
    sched.start()
    logger.info("scheduler started")
    try:
        await asyncio.Event().wait()
    finally:
        sched.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(_main())
