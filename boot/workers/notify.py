"""compose worker-notify 服务入口：跑对象事件通知队列 worker。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_notify_worker

assemble()


async def _main() -> None:
    await run_notify_worker()


if __name__ == "__main__":
    asyncio.run(_main())
