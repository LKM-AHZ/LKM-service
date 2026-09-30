"""compose worker-points-stats 服务入口：points 行为计数/成就订阅（扇出 2/3）。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_points_stats_worker

# 先装配（登记模型/任务/端口）再启动消费：否则 worker 会「未知任务 ack 丢弃」
assemble()


async def _main() -> None:
    await run_points_stats_worker()


if __name__ == "__main__":
    asyncio.run(_main())
