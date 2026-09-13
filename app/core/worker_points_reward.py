"""compose worker-points-reward 服务入口：points 奖励入账订阅（扇出 1/3）。"""

import asyncio

from app.core.worker import run_points_reward_worker


async def _main() -> None:
    await run_points_reward_worker()


if __name__ == "__main__":
    asyncio.run(_main())
