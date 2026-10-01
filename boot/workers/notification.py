"""compose worker-notification 服务入口：跑站内信生成 worker（M6.8）。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_notification_worker

assemble()


async def _main() -> None:
    await run_notification_worker()


if __name__ == "__main__":
    asyncio.run(_main())
