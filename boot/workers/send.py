"""compose worker-send 服务入口：跑发送队列 worker。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_send_worker

assemble()


async def _main() -> None:
    await run_send_worker()


if __name__ == "__main__":
    asyncio.run(_main())
