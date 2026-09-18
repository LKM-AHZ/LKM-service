"""内容互动计数的 Redis 链路落库与对账（M6.10）。

三项计数的**真相源是明细表**，``content_items`` 上的计数列是派生缓存：

- ``like_count``     ← ``content_likes`` 行数
- ``comment_count``  ← ``content_comments`` 行数
- ``bookmark_count`` ← ``interaction_favorites`` 行数

写路径把差值记进 Redis（``core.counters``），``flush_counters`` 周期把差值落到计数列，
``reconcile_counts`` 按明细行 ``COUNT(*)`` 重算并修正偏差（**可证伪**：连续两次对账，
第二次 ``affected == 0``）。

**不纳入本链路的计数**：``view_count``（浏览数）。它没有可重算的真相源——浏览明细
``interaction_view_logs`` 是「每用户每内容一行」的 upsert，行数与累计浏览次数不可换算，
对不上账。故 ``view_count`` 维持原有原子 UPDATE 直改 DB，登记于路线图 §8。

跨模块只读：``bookmark_count`` 的明细在 interaction 域，故 import
``interaction.models``（已在 pyproject 契约中精确豁免）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import counters
from app.core.err import BizError
from app.modules.content.errors import ContentErr
from app.modules.content.models import ContentComment, ContentItem, ContentLike
from app.modules.interaction.models import InteractionFavorite

# 计数列白名单：字段名 → ORM 列（同时用于 UPDATE 表达式构造）
_COLUMNS: dict[str, sa.Column[int]] = {
    "like_count": ContentItem.like_count,
    "comment_count": ContentItem.comment_count,
    "bookmark_count": ContentItem.bookmark_count,
}

# 真相源：字段名 → 明细表的 content_id 列
_DETAIL_SOURCES: dict[str, sa.Column[int]] = {
    "like_count": ContentLike.content_id,
    "comment_count": ContentComment.content_id,
    "bookmark_count": InteractionFavorite.content_id,
}


async def _direct_bump(
    db: AsyncSession, item_id: int, field: str, delta: int
) -> int:
    """fail-open 通道：原子 UPDATE 直改 DB（限定下限 0），返回新值。"""
    col = _COLUMNS[field]
    result = await db.execute(
        sa.update(ContentItem)
        .where(ContentItem.id == item_id)
        .values(**{field: func.greatest(col + delta, 0)})
        .returning(col)
    )
    row = result.first()
    if row is None:
        raise BizError(ContentErr.CONTENT_NOT_FOUND)
    return int(row[0])


async def bump_content_counter(
    db: AsyncSession, item_id: int, field: str, delta: int
) -> int:
    """记一次计数增减，返回**即时读数**（DB 值 + 未落库差值）。

    Redis 可用 → 只写增量（DB 计数列由 flush 收敛）；Redis 不可用 → 原路原子 UPDATE，
    语义与引入本链路前完全一致。
    """
    if field not in _COLUMNS:
        raise ValueError(f"unsupported counter field: {field!r}")

    if await counters.bump_counter(field, item_id, delta):
        col = _COLUMNS[field]
        base = await db.scalar(select(col).where(ContentItem.id == item_id))
        if base is None:
            raise BizError(ContentErr.CONTENT_NOT_FOUND)
        return int(base) + await counters.pending_delta(field, item_id)

    return await _direct_bump(db, item_id, field, delta)


async def read_count(db: AsyncSession, item_id: int, field: str) -> int:
    """即时读数：DB 计数列 + 未落库差值（Redis 不可用时即 DB 值）。"""
    col = _COLUMNS[field]
    base = await db.scalar(select(col).where(ContentItem.id == item_id))
    if base is None:
        raise BizError(ContentErr.CONTENT_NOT_FOUND)
    return int(base) + await counters.pending_delta(field, item_id)


async def flush_counters(db: AsyncSession) -> int:
    """把 Redis 里的待落库差值刷进计数列，返回被更新的行数。

    取走（``GETDEL``）即清零：若本次 DB 写入失败，差值丢失而非重复计入（宁少不重），
    偏差由 ``reconcile_counts`` 收敛。
    """
    drained = await counters.drain_counters()
    if not drained:
        return 0

    applied = 0
    for (field, item_id), delta in drained.items():
        if field not in _COLUMNS or not delta:
            continue
        col = _COLUMNS[field]
        result = await db.execute(
            sa.update(ContentItem)
            .where(ContentItem.id == item_id)
            .values(**{field: func.greatest(col + delta, 0)})
        )
        applied += int(result.rowcount or 0)
    return applied


async def reconcile_counts(
    db: AsyncSession, batch_size: int = 500
) -> tuple[int, int]:
    """按明细表重算三项计数并修正偏差，返回 ``(scanned, affected)``。

    以 ``id`` 键集分窗（每窗一条聚合查询 + 至多 N 条修正 UPDATE），内存与命令数有界。
    只更新**确有偏差**的行，并把 ``counts_reconciled_at`` 记为该行被修正的时刻——
    因此连续两次对账第二次 ``affected == 0``，收敛可证伪（见 tests/test_counters.py）。
    """
    scanned = 0
    affected = 0
    last_id = 0
    while True:
        real_cols = [
            select(func.count())
            .select_from(ContentLike)
            .where(ContentLike.content_id == ContentItem.id)
            .scalar_subquery()
            .label("real_like"),
            select(func.count())
            .select_from(ContentComment)
            .where(ContentComment.content_id == ContentItem.id)
            .scalar_subquery()
            .label("real_comment"),
            select(func.count())
            .select_from(InteractionFavorite)
            .where(InteractionFavorite.content_id == ContentItem.id)
            .scalar_subquery()
            .label("real_bookmark"),
        ]
        rows = (
            await db.execute(
                select(
                    ContentItem.id,
                    ContentItem.like_count,
                    ContentItem.comment_count,
                    ContentItem.bookmark_count,
                    *real_cols,
                )
                .where(ContentItem.id > last_id)
                .order_by(ContentItem.id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            break

        for row in rows:
            item_id = int(row[0])
            current = (int(row[1]), int(row[2]), int(row[3]))
            real = (int(row[4]), int(row[5]), int(row[6]))
            if real == current:
                continue
            await db.execute(
                sa.update(ContentItem)
                .where(ContentItem.id == item_id)
                .values(
                    like_count=real[0],
                    comment_count=real[1],
                    bookmark_count=real[2],
                    counts_reconciled_at=func.now(),
                )
            )
            affected += 1

        scanned += len(rows)
        last_id = int(rows[-1][0])

    return scanned, affected
