"""``points_ledger`` 的 continuous aggregate（度量/行为报表）装配与读口。

覆盖「装配 → 写入 → 刷新 → 读口」全链路，并**按扩展存在性分叉**（不 mock，与
``test_init_db.py::test_timescale_assembly_is_optional_and_non_fatal`` 同款）：

- 无 ``timescaledb``（CI 的 ``postgres:16-alpine``）：装配整体降级为空，读口抛
  :class:`PointsAggregateUnavailable`（路由据此返 503，绝不返回空列表冒充「无数据」）；
- 有 ``timescaledb``（compose 主库镜像）：走真路径，断言日桶聚合正确。

测试库是**克隆自模板库的独立 database**，模板库不带 ``timescaledb`` 扩展，故真路径用例
自己 ``CREATE EXTENSION`` 并就地装配（需超级用户——测试库用户即超级用户）。

三个 TimescaleDB 行为约束在本文件里被真实验证：

1. cagg 只能建在 **hypertable** 上 → ``_ensure_hypertables`` 的结果必须先 ``commit``，
   否则另开的连接看不到转换；
2. ``CREATE MATERIALIZED VIEW ... continuous`` **不能在事务块内** → 装配由
   ``_ensure_continuous_aggregates`` 自开 AUTOCOMMIT 连接完成；
3. ``refresh_continuous_aggregate`` 同样不能在事务块内 → 本文件另开 autocommit 引擎刷新。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.admin.points_report import (
    PointsAggregateUnavailable,
    points_daily_report,
)

_CAGG = "points_daily"


def _autocommit_engine(db: AsyncSession) -> AsyncEngine:
    """按测试会话所在库的 DSN 另开一个引擎（供 autocommit 用）。

    ``db.get_bind()`` 返回的是 **sync façade**（不是 ``AsyncEngine``），而 ``db`` 本身在事务里、
    不能在原连接上改 ``isolation_level``——故只能另开。
    """
    dsn = db.get_bind().url.render_as_string(hide_password=False)
    return create_async_engine(dsn, poolclass=NullPool)


async def test_points_continuous_aggregate_assembly_and_read(
    db: AsyncSession,
) -> None:
    """有 timescaledb：装配 cagg → 写一笔积分 → 刷新 → 读口看到当天桶；无则降级。"""
    from core.db.init_db import (
        _ensure_continuous_aggregates,
        _ensure_hypertables,
        _ensure_timescaledb,
    )

    conn = await db.connection()
    available = await _ensure_timescaledb(conn)
    engine = _autocommit_engine(db)

    try:
        if not available:
            # 降级路径：装配返回空（逐条告警跳过），读口明确报「不可用」而非空数据
            assert await _ensure_continuous_aggregates(engine) == []
            with pytest.raises(PointsAggregateUnavailable):
                await points_daily_report(db, days=7)
            return

        # 真路径：先 hypertable 再 cagg（顺序不可颠倒），且必须提交后才被另开的连接看见
        await _ensure_hypertables(conn)
        await db.commit()
        changed = await _ensure_continuous_aggregates(engine)
        assert f"cagg:{_CAGG}" in changed

        # 造一笔今天的积分流水（created_at 走模型 default）
        from app.modules.points.models import PointsLedger

        db.add(
            PointsLedger(
                user_id=uuid.uuid4(),
                delta=10,
                balance_after=10,
                reason="post",
                ref_type="post",
                ref_id="cagg-e2e-1",
            )
        )
        # 必须先提交：refresh 走**另一个连接**，看不见未提交行
        await db.commit()

        async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as raw:
            await raw.execute(
                text(f"CALL refresh_continuous_aggregate('{_CAGG}', NULL, NULL)")
            )

        points = await points_daily_report(db, days=1)
        today = datetime.datetime.now(datetime.UTC).date()
        assert any(
            p.day == today and p.reason == "post" and p.delta_sum == 10 for p in points
        ), points
    finally:
        await engine.dispose()


async def test_points_report_filters_by_reason(db: AsyncSession) -> None:
    """``reason`` 过滤把窗口缩到单一行为类型（降级时同样走异常路径）。"""
    from core.db.init_db import (
        _ensure_continuous_aggregates,
        _ensure_hypertables,
        _ensure_timescaledb,
    )

    conn = await db.connection()
    engine = _autocommit_engine(db)
    try:
        if not await _ensure_timescaledb(conn):
            with pytest.raises(PointsAggregateUnavailable):
                await points_daily_report(db, days=7, reason="post")
            return

        await _ensure_hypertables(conn)
        await db.commit()
        await _ensure_continuous_aggregates(engine)

        # 未知 reason 只会查空，不报错、不落到全量
        assert await points_daily_report(db, days=7, reason="__nonexistent__") == []
    finally:
        await engine.dispose()


async def test_points_report_rejects_nonpositive_days(db: AsyncSession) -> None:
    """``days<=0`` 在窗口计算前就被拒（0 会退化成空窗口、负数是不合法区间）。"""
    with pytest.raises(ValueError):
        await points_daily_report(db, days=0)
