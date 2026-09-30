"""后台报表端点：``/admin/points-report``（积分度量/行为报表，离线读 cagg）。

蓝图目标篇「PG 连续聚合视图（度量/行为报表）」的 HTTP 面。只读；须后台会话
（``require_admin``）+ ``admin.analytics_view`` 权限。

**读的是连续聚合物化视图**，按策略刷新、天然滞后（装配为 1 小时刷新 + ``end_offset``
1 小时）——报表面容忍它；实时积分/排行榜请走 ``/points/leaderboard``。非 TimescaleDB 实例上
视图不存在 → 503（``CommonErr.UNAVAILABLE``），**不返回空列表冒充「无数据」**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import BizError, CommonErr, respond
from app.db.session import get_read_session
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser

from .deps import require_admin
from .permissions import require_permission
from .points_report import PointsAggregateUnavailable, points_daily_report
from .schemas import PointsReportOut

router = APIRouter(prefix="/admin", tags=["admin-data"])


@router.get("/points-report", response_model=ApiResp[PointsReportOut])
@respond
async def admin_points_report(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    # 行为类型过滤（points/rules.py::RULE_DELTAS 的键），非白名单——未知值只会查空
    reason: Annotated[str | None, Query(max_length=50)] = None,
    _cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_read_session),
) -> PointsReportOut:
    """积分发放的日桶 × 行为类型报表（离线面，读 continuous aggregate）。

    返回按 ``day``/``reason`` 升序的序列与合计；``days`` 为窗口（含今天，UTC 日对齐）。
    """
    await require_permission(db, _cur, Permission.admin_analytics_view)
    try:
        series = await points_daily_report(db, days=days, reason=reason)
    except PointsAggregateUnavailable as exc:
        raise BizError(
            CommonErr.UNAVAILABLE,
            "points report requires a TimescaleDB continuous aggregate",
        ) from exc
    return PointsReportOut(
        days=days,
        series=series,
        total_delta=sum(point.delta_sum for point in series),
        total_entries=sum(point.entry_count for point in series),
    )
