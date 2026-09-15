"""event_failures → ClickHouse 增量导出（M5 7.2.6 路 A，owner 侧）。

只读业务库 ``event_failures``（outbox relay 耗竭归档），按 CH 侧 ``max(id)`` 水位增量
批量导出。client 由调用方注入（flow 层建连），本模块不自持连接，便于单测。

不变量（对齐 ``user_dim_sync`` 的 ETL 纪律）：
- **命令数恒定**：每拍 = 1 次水位查询 + 每批 1 次 PG 查询 + 1 次 CH insert，绝不逐行；
- **批量幂等**：水位推进 + CH ``ReplacingMergeTree`` 双保险，重跑 diff=0；
- **窗口分批**：单批满 ``window`` 继续下一批，不足即收敛。

失败语义：CH 不可达/insert 抛错时向上抛（导出侧必须重试或显式失败），绝不静默吞掉。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clickhouse import (
    ClickHouseClient,
    fetch_watermark,
    to_ch_datetime,
)
from app.db.event_failure import EventFailure

CH_TABLE = "lkm.event_failures"
CH_COLUMNS: tuple[str, ...] = (
    "id",
    "event_id",
    "routing_key",
    "payload_json",
    "attempt_count",
    "reason",
    "folded_at",
)


def _to_row(row: EventFailure) -> tuple[Any, ...]:
    """ORM 行 → CH 列元组（payload dict 序列化为 JSON 串，时间转 naive UTC）。"""
    return (
        row.id,
        row.event_id,
        row.routing_key,
        json.dumps(row.payload_json, ensure_ascii=False),
        row.attempt_count,
        row.reason,
        to_ch_datetime(row.folded_at),
    )


async def export_event_failures(
    db: AsyncSession,
    client: ClickHouseClient,
    *,
    window: int = 1000,
    max_batches: int = 10000,
) -> int:
    """把水位之后未导出的 event_failures 分批灌入 CH，返回本次导出总行数。

    幂等：以 CH 当前 ``max(id)`` 为水位，重跑时已导出部分不再命中；CH 侧
    ReplacingMergeTree 对同 id 覆盖写，双保险。``max_batches`` 防高写入下无界循环，
    超出即返回，剩余由下次周期继续。
    """
    watermark = await fetch_watermark(client, CH_TABLE)
    total = 0
    for _ in range(max_batches):
        rows = (
            (
                await db.execute(
                    select(EventFailure)
                    .where(EventFailure.id > watermark)
                    .order_by(EventFailure.id)
                    .limit(window)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        await client.insert(CH_TABLE, [_to_row(r) for r in rows], list(CH_COLUMNS))
        total += len(rows)
        watermark = rows[-1].id
        if len(rows) < window:
            break
    return total
