"""compose worker-points-reward 服务入口：points 奖励入账订阅（扇出 1/3）。"""

import asyncio

from boot.assemble import assemble
from core.worker import run_points_reward_worker

# 先装配（登记模型/任务/端口）再启动消费：否则 worker 会「未知任务 ack 丢弃」
assemble()


async def _main() -> None:
    await run_points_reward_worker()


if __name__ == "__main__":
    asyncio.run(_main())
