"""后台报表端点：``/admin/user-dim``（离线用户宽表分页读）。

蓝图 §5.4 定案 ``user_dim`` 宽表「仅服务运营报表 / 后台管理列表」。本端点即那个消费面——
在此之前 ``dim_report.list_user_dim`` **只有测试直调**，ETL 灌的这张表没有任何真实读者。

只读；须后台会话（``require_admin``）+ ``admin.analytics_view`` 权限；**固定不带 PII**
（``include_pii=False``）——报表面不开口子。要实时准确的用户数据请走 A4 用户列表。
"""

from fastapi import APIRouter, Depends, Query
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
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser

from .deps import require_admin
from .dim_report import list_user_dim
from .permissions import require_permission
from .schemas import DimUserRow

router = APIRouter(prefix="/admin", tags=["admin-data"])


@router.get("/user-dim", response_model=ApiResp[PageData[DimUserRow]])
@respond
async def admin_list_user_dim(
    # 关键字只按用户名匹配（与 A4 用户列表同口径，不做泄露式的按邮箱枚举）
    q: str | None = Query(default=None, max_length=64),
    _cur: CurrentUser = require_admin,
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[DimUserRow]:
    """离线用户宽表分页列表（按用户名模糊匹配，id 倒序）。

    读的是 ``user_dim`` **离线副本**（ETL 单向灌入，允许分钟级滞后），不是在线 auth 真值；
    ``sync_ts`` 供调用方判断每行的物化新鲜度。
    """
    await require_permission(db, _cur, Permission.admin_analytics_view)
    items, total = await list_user_dim(
        db, q=q, offset=pag.offset, limit=pag.limit, include_pii=False
    )
    return PageData(
        items=items,
        total=total,
        page=pag.page,
        pages=paginate_pages(total, pag.limit),
    )
