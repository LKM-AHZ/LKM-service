"""compose worker-scheduler 服务入口：跑 APScheduler（cron 触发投递到消息总线）。"""

import asyncio
import logging

from app.core.scheduler import build_scheduler
from app.core.tracing import setup_tracing

logger = logging.getLogger("lkm.scheduler")


async def _main() -> None:
    # 调度进程不是 ASGI app：初始化 provider，让 cron 触发的 span 能导出（默认关时 no-op）
    setup_tracing(service_suffix="-scheduler")

    sched = build_scheduler()
    sched.start()
    logger.info("scheduler started")
    try:
        await asyncio.Event().wait()
    finally:
        sched.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(_main())
