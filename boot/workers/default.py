"""compose worker 服务入口：跑默认队列 worker。"""

import asyncio
import logging

from boot.assemble import assemble
from core.logging import setup_logging
from core.worker import run_default_worker

assemble()

logger = logging.getLogger("lkm.worker_default")


async def _main() -> None:
    setup_logging()
    try:
        await run_default_worker()
    except Exception:
        logger.exception("默认 worker 异常退出")
        raise


if __name__ == "__main__":
    asyncio.run(_main())
