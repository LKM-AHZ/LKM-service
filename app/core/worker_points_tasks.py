"""compose worker-points-tasks 服务入口：points 每日任务推进订阅（扇出 3/3）。"""

import asyncio

from app.core.worker import run_points_tasks_worker


async def _main() -> None:
    await run_points_tasks_worker()


if __name__ == "__main__":
    asyncio.run(_main())
