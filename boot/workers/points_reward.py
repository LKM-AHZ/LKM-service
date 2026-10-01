"""compose worker-points-reward 服务入口：points 奖励入账订阅（扇出 1/3）。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_points_reward_worker

assemble()


async def _main() -> None:
    await run_points_reward_worker()


if __name__ == "__main__":
    asyncio.run(_main())
