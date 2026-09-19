"""后台分析查询端点：/admin/analytics/{dataset}（M5 7.2.6）。

后台只读端点，须持有有效后台 cookie 会话（``require_admin``）且持有 ``admin.analytics_view``
权限点。查询 ClickHouse 分析库，**表名/列名/排序全部为代码内白名单常量**，过滤条件与分页
一律参数化——绝不接受用户可控 SQL 片段，不存在任意 SQL 面。

CH 未启用/不可达 → 503（``CommonErr.UNAVAILABLE``），**绝不返回空列表冒充「无数据」**
（避免运维误判为「没有日志/失败」而掩盖后端故障）。

注：ReplacingMergeTree 表在后台合并前 ``count()`` 可能短暂含重复；导出口径的
``max(id)`` 水位已保证不重导，故实际重复窗口极小，首期接受（大表再改 ``uniqExact``）。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clickhouse
from app.core.clickhouse import (
    ClickHouseClient,
    ClickHouseUnavailableError,
    result_rows,
)
from app.core.common import ApiResp, PageData, paginate_pages
from app.core.config import settings
from app.core.err import BizError, CommonErr, respond
from app.db.session import get_read_session
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser

from .deps import require_admin
from .permissions import require_permission

router = APIRouter(prefix="/admin", tags=["admin-analytics"])


@dataclass(frozen=True)
class DatasetSpec:
    """白名单数据集：唯一允许查询的表、时间列、投影列与排序。"""

    table: str
    time_column: str
    columns: tuple[str, ...]
    order_by: str


# 白名单：任何外部输入都不得进入标识符位置（表/列/排序均取自此表）。
DATASETS: dict[str, DatasetSpec] = {
    "app_logs": DatasetSpec(
        "lkm.app_logs",
        "ts",
        (
            "ts",
            "level",
            "logger",
            "msg",
            "request_id",
            "trace_id",
            "span_id",
            "service",
        ),
        "ts DESC",
    ),
    "event_failures": DatasetSpec(
        "lkm.event_failures",
        "folded_at",
        ("id", "event_id", "routing_key", "attempt_count", "reason", "folded_at"),
        "id DESC",
    ),
    "audit_logs": DatasetSpec(
        "lkm.audit_logs",
        "created_at",
        ("id", "user_id", "action", "detail", "ip_address", "created_at"),
        "id DESC",
    ),
}


async def get_analytics_client() -> ClickHouseClient:
    """依赖：取 CH 客户端；未启用/不可达抛 503（不吞成空数据）。"""
    try:
        return await clickhouse.get_client()
    except ClickHouseUnavailableError as exc:
        raise BizError(
            CommonErr.UNAVAILABLE, "ClickHouse analytics backend unavailable"
        ) from exc


@router.get("/analytics/{dataset}", response_model=ApiResp[PageData[dict[str, Any]]])
@respond
async def admin_query_analytics(
    dataset: str,
    since: datetime.datetime | None = Query(default=None),
    until: datetime.datetime | None = Query(default=None),
    # 不用 PaginateDep：CH 单页上限由 LKM_CLICKHOUSE_QUERY_LIMIT_MAX 独立配置
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    _cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_read_session),
    client: ClickHouseClient = Depends(get_analytics_client),
) -> PageData[dict[str, Any]]:
    """按白名单数据集查询分析库（时间窗 + 分页），过滤与分页全参数化。"""
    await require_permission(db, _cur, Permission.admin_analytics_view)
    spec = DATASETS.get(dataset)
    if spec is None:
        raise BizError(CommonErr.INVALID_INPUT, f"unknown dataset: {dataset}")

    page_limit = min(limit, settings.clickhouse_query_limit_max)
    offset = (page - 1) * page_limit

    where: list[str] = []
    params: dict[str, Any] = {}
    if since is not None:
        where.append(f"{spec.time_column} >= {{since:DateTime64(3)}}")
        params["since"] = since
    if until is not None:
        where.append(f"{spec.time_column} <= {{until:DateTime64(3)}}")
        params["until"] = until
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    total_rows = result_rows(
        await client.query(f"SELECT count() FROM {spec.table}{where_sql}", params)
    )
    total = int(total_rows[0][0]) if total_rows else 0

    params["lim"] = page_limit
    params["off"] = offset
    result = await client.query(
        f"SELECT {', '.join(spec.columns)} FROM {spec.table}{where_sql}"
        f" ORDER BY {spec.order_by} LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}",
        params,
    )
    columns = list(getattr(result, "column_names", spec.columns))
    items = [dict(zip(columns, row, strict=False)) for row in result_rows(result)]

    return PageData(
        items=items,
        total=total,
        page=page,
        pages=paginate_pages(total, page_limit),
    )
