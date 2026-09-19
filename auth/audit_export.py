"""audit_logs → ClickHouse 增量导出（M5 7.2.6 路 A，auth owner 侧）。

``audit_logs`` 属 auth 独立库（``AuthBase``），故导出入口必须留在 auth 域内（业务域
不得 import auth 表，见 import-linter 契约四）。client 由调用方注入（flow 层建连），
本模块不自持连接。

不变量同 ``app/db/event_failure_export.py``：命令数恒定、CH ``max(id)`` 水位增量、
``ReplacingMergeTree`` 幂等；失败向上抛，绝不静默。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clickhouse import (
    ClickHouseClient,
    fetch_watermark,
    to_ch_datetime,
)
from auth.models import AuditLog

CH_TABLE = "lkm.audit_logs"
CH_COLUMNS: tuple[str, ...] = (
    "id",
    "user_id",
    "action",
    "detail",
    "ip_address",
    "created_at",
)


def _to_row(row: AuditLog) -> tuple[Any, ...]:
    """ORM 行 → CH 列元组（uuid 落字符串形式，可空文本列落空串，时间转 naive UTC）。"""
    return (
        str(row.id),
        None if row.user_id is None else str(row.user_id),
        row.action,
        row.detail or "",
        row.ip_address or "",
        to_ch_datetime(row.created_at),
    )


async def export_audit_logs(
    db: AsyncSession,
    client: ClickHouseClient,
    *,
    window: int = 1000,
    max_batches: int = 10000,
) -> int:
    """把水位之后未导出的 audit_logs 分批灌入 CH，返回本次导出总行数。

    ``max_batches`` 防高写入下无界循环；超出即返回，剩余由下次周期继续。主键为 uuid7，
    水位取字符串形式（CH String 列，字典序即时间序）；CH 空表返回 ``None`` 时首次全量。
    """
    watermark = await fetch_watermark(client, CH_TABLE)
    total = 0
    for _ in range(max_batches):
        stmt = select(AuditLog).order_by(AuditLog.id).limit(window)
        if watermark is not None:
            # 首次导出（CH 空表）无水位 → 不加条件即全量，语义同原 watermark=0。
            # watermark 是 uuid7 字符串，与 Uuid 列比较时按原生 uuid 绑定。
            stmt = stmt.where(AuditLog.id > watermark)
        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            break
        await client.insert(CH_TABLE, [_to_row(r) for r in rows], list(CH_COLUMNS))
        total += len(rows)
        watermark = str(rows[-1].id)
        if len(rows) < window:
            break
    return total
