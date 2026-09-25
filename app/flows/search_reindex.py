"""Prefect flow：检索索引全量重建（B2）。

存量与「引擎侧写入丢失」的兜底：日常增量由 ``content.*`` 事件驱动（``search/sync.py``），
本 flow 负责把全部已发布内容重灌一遍。纯体层在 ``search_reindex_body.py``（无 Prefect 依赖），
本文件只加 task/flow 外壳与 CLI。

CLI（在 worker / prefect-worker 容器内手工重建）::

    python -m app.flows.search_reindex --batch-size 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from prefect import flow, task

from app.flows.search_reindex_body import rebuild_index

logger = logging.getLogger("lkm.flows.search_reindex")


@task(retries=2, retry_delay_seconds=60)
async def rebuild_index_task(batch_size: int | None = None) -> dict[str, Any]:
    return await rebuild_index(batch_size=batch_size)


@flow(name="search-reindex")
async def search_reindex_flow(batch_size: int | None = None) -> dict[str, Any]:
    return await rebuild_index_task(batch_size)


def main() -> None:
    parser = argparse.ArgumentParser(description="重建外部检索索引（全量）")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="每批写入条数（默认取 LKM_SEARCH_SYNC_BATCH_SIZE）",
    )
    args = parser.parse_args()
    result = asyncio.run(rebuild_index(batch_size=args.batch_size))
    logger.info("search reindex 完成: %s", result)
    print(result)


if __name__ == "__main__":
    main()
