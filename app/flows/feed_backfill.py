"""Prefect flow：时间线全量回填（B5）。

日常扩散由 cron 水位扫描覆盖；本 flow 用于**存量/灾后重建**（水位被清、物化表被清、
或首次上线时补历史）。纯体层在 ``feed_backfill_body.py``（无 Prefect 依赖），本文件只加
task/flow 外壳与 CLI。

CLI（在 worker / prefect-worker 容器内手工回填）::

    python -m app.flows.feed_backfill --since 2026-01-01T00:00:00+08:00 --batch-size 500
    python -m app.flows.feed_backfill            # 全量（自最早）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from prefect import flow, task

from app.flows.feed_backfill_body import backfill_feed

logger = logging.getLogger("lkm.flows.feed_backfill")


@task(retries=1, retry_delay_seconds=60)
async def backfill_feed_task(
    since: str | None = None, batch_size: int | None = None
) -> dict[str, Any]:
    return await backfill_feed(since=since, batch_size=batch_size)


@flow(name="feed-backfill")
async def feed_backfill_flow(
    since: str | None = None, batch_size: int | None = None
) -> dict[str, Any]:
    return await backfill_feed_task(since, batch_size)


def main() -> None:
    parser = argparse.ArgumentParser(description="回填关注者时间线物化 feed（存量）")
    parser.add_argument(
        "--since",
        default=None,
        help="起始时间（ISO 8601）；缺省取 LKM_FEED_BACKFILL_SINCE，为空则自最早",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="每源每批条数（默认取 LKM_FEED_BACKFILL_BATCH_SIZE）",
    )
    args = parser.parse_args()
    result = asyncio.run(backfill_feed(since=args.since, batch_size=args.batch_size))
    logger.info("feed backfill 完成: %s", result)
    print(result)


if __name__ == "__main__":
    main()
