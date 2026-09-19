"""compose worker-outbox 服务入口：跑 outbox relay 单 owner poller（M1.1）。"""

import asyncio

from app.core.outbox_relay import run_outbox_loop
from app.core.tracing import setup_tracing


async def _main() -> None:
    # relay 进程不是 ASGI app：初始化 provider，发布 span 与 inject 进消息的 trace
    # 上下文才可用（默认关时 no-op）
    setup_tracing(service_suffix="-outbox")
    await run_outbox_loop()


if __name__ == "__main__":
    asyncio.run(_main())
