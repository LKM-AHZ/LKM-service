"""兼容入口：单进程消费 points-reward 订阅（M4 前为单一 points 队列）。

M4 把 points 拆成 reward/stats/tasks 三个订阅，生产由三个独立进程分别消费
（``worker_points_reward`` / ``worker_points_stats`` / ``worker_points_tasks``），
以获得真实故障隔离。本入口保留仅供旧部署/调试，等价于只跑 reward 订阅。
"""

import asyncio

from app.core.worker import run_points_worker


async def _main() -> None:
    await run_points_worker()


if __name__ == "__main__":
    asyncio.run(_main())
