"""compose worker-content-index 服务入口：跑 content.* 事件索引同步 worker。

消费 ``content-index`` 订阅，把内容可见性变化增量同步到外部检索索引
（Meilisearch / OpenSearch，引擎由 ``LKM_SEARCH_ENGINE`` 择一）。
"""

import asyncio

from app.core.worker import run_content_index_worker


async def _main() -> None:
    await run_content_index_worker()


if __name__ == "__main__":
    asyncio.run(_main())
