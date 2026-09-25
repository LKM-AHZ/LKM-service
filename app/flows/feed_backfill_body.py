"""时间线全量回填的纯体层（B5，**无 Prefect 依赖**）。

日常扩散由 cron 水位扫描驱动（``feed/fanout.py``，每 2 分钟）；本层负责**存量回填**：
把各源水位回拨到指定时间（默认最早）后循环扩散至无积压。

**复用既有语义，不复制逻辑**：

- 幂等靠 ``feed_items`` 的 ``uq_feed_item`` 唯一约束 + ``on_conflict_do_nothing``（可重跑）；
- 大 V 封顶与实时补拉由 ``_fanout_item`` 原样承担——回填不会把大 V 内容写进物化；
- 水位推进由 ``fanout_batch`` 自己维护（每源每轮至多 ``per_source_limit`` 条）。

**边界**（登记 §8）：物化读路径把「物化行 + 大 V 实时补拉」直接拼接、不做 ``(item_type, id)``
去重，故回填只覆盖走物化的作者；大 V 条目仍由实时补拉提供，不会因回填而重复出现。
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.config import settings
from app.db.session import new_worker_session as new_session
from app.modules.feed import feed as feed_src
from app.modules.feed.fanout import fanout_batch
from app.modules.feed.models import FeedFanoutState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("lkm.flows.feed_backfill")

# 「最早」哨兵：PG timestamptz 的下界远早于此，用固定值而非 datetime.min 以免时区/精度问题
_EARLIEST = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


def _parse_since(raw: str | None) -> datetime.datetime:
    """解析起始时间；空 = 最早。非法值显式报错（静默回退会被当成「已回填完」）。"""
    text = (raw or "").strip()
    if not text:
        return _EARLIEST
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"起始时间无法解析为 ISO 8601（如 2026-01-01T00:00:00+08:00）: {raw!r}"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


async def _rewind_watermarks(db: AsyncSession, since: datetime.datetime) -> int:
    """回拨全部源的水位，并**为缺失的源补行**——否则未 fanout 过的源存量永远不回填。"""
    existing = {
        state.source: state
        for state in (await db.execute(select(FeedFanoutState))).scalars().all()
    }
    for name in feed_src.FOLLOW_SOURCES:
        state = existing.get(name)
        if state is None:
            db.add(FeedFanoutState(source=name, last_created_at=since, last_id=None))
        else:
            state.last_created_at = since
            state.last_id = None
    await db.flush()
    return len(feed_src.FOLLOW_SOURCES)


async def backfill_feed(
    *,
    since: str | None = None,
    batch_size: int | None = None,
    db: AsyncSession | None = None,
    max_batches: int | None = None,
) -> dict[str, Any]:
    """回填存量内容到关注者物化 feed，返回 ``{sources, fanout_items, batches}``。

    ``db`` 可注入（测试传同库会话；生产留空则自开自关）。``max_batches`` 限轮数（压测/分批
    跑用；生产全量留空跑到底）。
    """
    size = batch_size if batch_size else settings.feed_backfill_batch_size
    if size <= 0:
        raise ValueError(f"batch_size 必须为正数，实测 {size}")

    owned = db is None
    session = db if db is not None else await new_session()
    try:
        since_raw = since if since is not None else settings.feed_backfill_since
        sources = await _rewind_watermarks(session, _parse_since(since_raw))

        total = 0
        batches = 0
        while True:
            processed = await fanout_batch(session, per_source_limit=size)
            if processed == 0:
                break
            total += processed
            batches += 1
            if max_batches is not None and batches >= max_batches:
                break
        return {"sources": sources, "fanout_items": total, "batches": batches}
    finally:
        if owned:
            await session.close()
