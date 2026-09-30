"""compose worker-dlq 服务入口：消费死信队列并落库。

消费逻辑在 ``core.worker_dlq``（core 侧），但模型注册与端口绑定需要先装配——
故入口留在 boot，core 不再提供 ``__main__``。
"""

import asyncio

from boot.assemble import assemble

# 先装配（登记模型/任务/端口）再启动消费：否则 worker 会「未知任务 ack 丢弃」
assemble()

from core.worker_dlq import consume_dlq  # noqa: E402


async def _main() -> None:
    await consume_dlq()


if __name__ == "__main__":
    asyncio.run(_main())
