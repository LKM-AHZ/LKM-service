"""compose worker-notification 服务入口：跑站内信生成 worker（M6.8）。"""

import asyncio

from app.core.worker import run_notification_worker


async def _main() -> None:
    await run_notification_worker()


if __name__ == "__main__":
    asyncio.run(_main())
