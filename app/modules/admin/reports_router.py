"""后台举报端点：/admin/reports（举报列表）。

后台只读端点，须持有有效后台 cookie 会话（require_admin）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import (
    ApiResp,
    PageData,
    PaginateDep,
    PaginateParams,
    paginate_pages,
)
from app.core.err import respond
from app.db.session import get_read_session
from app.modules.admin.models import Report
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser

from .deps import require_admin
from .permissions import require_permission
from .schemas import AdminReportListItem

router = APIRouter(prefix="/admin", tags=["admin-data"])


@router.get("/reports", response_model=ApiResp[PageData[AdminReportListItem]])
@respond
async def admin_list_reports(
    # 白名单：拼错/大小写不对的状态原先静默返回空页，调用方分不清「没有举报」与「筛选值写错」
    status: str | None = Query(
        default=None, pattern="^(pending|resolved|dismissed)$"
    ),
    _cur: CurrentUser = require_admin,
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[AdminReportListItem]:
    """
    举报审核列表。可按状态过滤（pending/resolved/dismissed），倒序分页。
    """
    await require_permission(db, _cur, Permission.admin_reports_view)
    query = select(Report)
    count_q = select(func.count(Report.id))

    if status:
        query = query.where(Report.status == status)
        count_q = count_q.where(Report.status == status)

    total = (await db.execute(count_q)).scalar() or 0
    rows = (
        await db.execute(
            query.order_by(Report.id.desc()).offset(pag.offset).limit(pag.limit)
        )
    ).scalars()

    items = [
        AdminReportListItem(
            id=r.id,
            type=str(r.type),
            target_id=str(r.target_id),
            target_title=str(r.target_title),
            reporter_name=str(r.reporter_name),
            reason=str(r.reason),
            status=str(r.status),
            created_at=r.created_at,
            handled_at=r.handled_at,
        )
        for r in rows
    ]

    return PageData(
        items=items,
        total=total,
        page=pag.page,
        pages=paginate_pages(total, pag.limit),
    )
