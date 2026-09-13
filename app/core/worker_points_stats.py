"""compose worker-points-stats 服务入口：points 行为计数/成就订阅（扇出 2/3）。"""

import asyncio

from app.core.worker import run_points_stats_worker


async def _main() -> None:
    await run_points_stats_worker()


if __name__ == "__main__":
    asyncio.run(_main())
