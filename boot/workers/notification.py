"""compose worker-notification 服务入口：跑站内信生成 worker（M6.8）。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_notification_worker

# 先装配（登记模型/任务/端口）再启动消费：否则 worker 会「未知任务 ack 丢弃」
assemble()


async def _main() -> None:
    await run_notification_worker()


if __name__ == "__main__":
    asyncio.run(_main())
