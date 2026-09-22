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
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clickhouse import (
    ClickHouseClient,
    fetch_watermark,
    to_ch_datetime,
)
from app.db.event_failure import EventFailure

logger = logging.getLogger("lkm.db.event_failure_export")

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
    """ORM 行 → CH 列元组（uuid 落字符串形式，payload 序列化为 JSON，时间转 naive UTC）。"""
    return (
        str(row.id),
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
    超出即返回，剩余由下次周期继续。主键为 uuid7，水位取字符串形式（CH String 列，
    字典序即时间序）；CH 空表返回 ``None`` 时首次全量导出。
    """
    watermark = await fetch_watermark(client, CH_TABLE)
    total = 0
    for _ in range(max_batches):
        stmt = select(EventFailure).order_by(EventFailure.id).limit(window)
        if watermark is not None:
            # 首次导出（CH 空表）无水位 → 不加条件即全量，语义同原 watermark=0。
            # watermark 是 uuid7 字符串，与 Uuid 列比较时按原生 uuid 绑定。
            stmt = stmt.where(EventFailure.id > watermark)
        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            break
        await client.insert(CH_TABLE, [_to_row(r) for r in rows], list(CH_COLUMNS))
        total += len(rows)
        watermark = str(rows[-1].id)
        if len(rows) < window:
            break
    else:
        # for 正常跑完（未 break）= 用满 max_batches 仍有积压：返回值与「追上水位」同形，
        # 调用方只看正数分不出来，故显式告警——持续写入速率高于每轮导出量时积压会永久落后
        logger.warning(
            "event_failures 导出达到 max_batches=%d，水位=%s，剩余积压留待下轮",
            max_batches,
            watermark,
        )
    return total
