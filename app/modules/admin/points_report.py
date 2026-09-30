"""admin 离线报表读口：积分度量/行为报表（读 continuous aggregate ``points_daily``）。

蓝图目标篇「PG 连续聚合视图（度量/行为报表）」的消费面。``points_daily`` 由
``app/db/init_db.py``（create_all 通道）或 ``alembic/versions/0004_*``（迁移通道）装配在
hypertable ``points_ledger`` 之上，按 **日桶 × reason** 预聚合积分发放量（``reason`` 即行为
类型）。本模块是该视图的**唯一 intended 读口**。

**离线边界**：连续聚合按策略刷新，读出来**天然滞后**（装配为 1 小时刷新 + ``end_offset``
1 小时）——报表面容忍它，实时真值请走 ``/points/leaderboard``。与 ``dim_report`` 的
「离线报表 vs 在线实时」边界同款。

**降级**：非 TimescaleDB 实例（普通 PG 镜像 / CI 临时 PG）上视图**不存在** → 抛
:class:`PointsAggregateUnavailable`，由路由转 503。**绝不返回空列表冒充「无数据」**——那会把
「环境没有连续聚合」误报成「这段时间没有任何积分行为」，与 ``analytics_router`` 对
ClickHouse 不可用的处理同款。

**查询表达**：经 SQLAlchemy Core 表达（``sa.table()`` 轻量构造 + 全参数化条件），既无字符串
拼接也无裸 SQL（``scripts/check_raw_sql.py`` 门禁）。视图**不能**注册进 ``Base.metadata``——
那会让 ``create_all`` 试图把物化视图当普通表建出来。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    String,
    column,
    func,
    select,
    table,
)
from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import PointsReportPoint

# 白名单常量：标识符位置不接受任何外部输入（表名/列名全在代码内）。
_RELATION = "points_daily"

# 轻量只读构造：**不是** Base.metadata 里的模型——物化视图由 TimescaleDB 装配。
_POINTS_DAILY = table(
    _RELATION,
    column("bucket", DateTime(timezone=True)),
    column("reason", String(50)),
    column("delta_sum", BigInteger),
    column("entry_count", BigInteger),
)


class PointsAggregateUnavailable(RuntimeError):
    """continuous aggregate 未装配（非 TimescaleDB 实例）或不可查询。"""


def _window_start(days: int) -> datetime.datetime:
    """近 ``days`` 个 UTC 日的起点（含今天），与 cagg 的 UTC 日桶对齐。"""
    today = datetime.datetime.now(datetime.UTC).date()
    return datetime.datetime.combine(
        today - datetime.timedelta(days=days - 1),
        datetime.time.min,
        tzinfo=datetime.UTC,
    )


async def points_daily_report(
    db: AsyncSession, *, days: int, reason: str | None = None
) -> list[PointsReportPoint]:
    """读 ``points_daily`` 的日桶序列（按 bucket 升序），``reason`` 可选过滤。"""
    if days <= 0:
        raise ValueError(f"days 必须为正数，收到 {days}")

    # 先探视图存在性：直接查不存在的视图会让会话进 InFailedSQLTransaction，且异常映射依赖
    # 驱动细节；``to_regclass`` 给出确定的「装配与否」判据（按 search_path 解析）。
    if await db.scalar(select(func.to_regclass(_RELATION))) is None:
        raise PointsAggregateUnavailable(
            f"continuous aggregate {_RELATION} 未装配（需要 TimescaleDB）"
        )

    stmt = (
        select(
            _POINTS_DAILY.c.bucket,
            _POINTS_DAILY.c.reason,
            _POINTS_DAILY.c.delta_sum,
            _POINTS_DAILY.c.entry_count,
        )
        .where(_POINTS_DAILY.c.bucket >= _window_start(days))
        .order_by(_POINTS_DAILY.c.bucket, _POINTS_DAILY.c.reason)
    )
    if reason is not None:
        stmt = stmt.where(_POINTS_DAILY.c.reason == reason)

    rows = (await db.execute(stmt)).all()
    return [
        PointsReportPoint(
            day=bucket.date(),
            reason=row_reason,
            delta_sum=int(delta_sum),
            entry_count=int(entry_count),
        )
        for bucket, row_reason, delta_sum, entry_count in rows
    ]
