"""compose worker-content-index 服务入口：跑 content.* 事件索引同步 worker。

消费 ``content-index`` 订阅，把内容可见性变化增量同步到外部检索索引
（Meilisearch / OpenSearch，引擎由 ``LKM_SEARCH_ENGINE`` 择一）。
"""

import asyncio

from boot.assemble import assemble
from core.worker import run_content_index_worker

# 先装配（登记模型/任务/端口）再启动消费：否则 worker 会「未知任务 ack 丢弃」
assemble()


async def _main() -> None:
    await run_content_index_worker()


if __name__ == "__main__":
    asyncio.run(_main())
