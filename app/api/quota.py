"""横切只读配额端点：``GET /api/v1/quotas/me``。

统一各资源的 ``{resource, used, limit, remaining}`` 结构（DysonNetwork ``ResourceQuotaResponse``
的 LKM 版）。上限来自 Settings，未配置（``None``）即不限，``limit``/``remaining`` 同为 null。

统计一律走 ``core.ports``：配额路由不认识任何业务 ORM，也不新增业务模块。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.common import ApiResp
from core.config import settings
from core.contracts import CurrentUser
from core.db.session import get_read_session
from core.err import BizError, CommonErr, respond
from core.ports import columns as columns_port
from core.ports import files as files_port
from core.ports import projects as projects_port
from core.ports.authz import get_optional_user

router = APIRouter(prefix="/quotas", tags=["quotas"])


class QuotaItem(BaseModel):
    """单项配额：``limit`` 为空 = 未配置上限，``remaining`` 随之也为空。"""

    resource: str
    used: int
    limit: int | None = None
    remaining: int | None = None


class QuotaData(BaseModel):
    items: list[QuotaItem]


def _item(resource: str, used: int, limit: int | None) -> QuotaItem:
    return QuotaItem(
        resource=resource,
        used=used,
        limit=limit,
        remaining=None if limit is None else max(limit - used, 0),
    )


@router.get("/me", response_model=ApiResp[QuotaData])
@respond
async def my_quotas(
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> QuotaData:
    # 配额是「我的」资源：缺令牌/令牌无效统一 401（复用的 get_current_user 对缺头报 403，
    # 属全站旧口径；本端点按标准语义显式返回 401）。
    if cur is None:
        raise BizError(CommonErr.UNAUTHORIZED)
    return QuotaData(
        items=[
            _item(
                "files.count",
                await files_port.count_for_user(db, cur.id),
                settings.quota_files_count,
            ),
            _item(
                "projects.active",
                await projects_port.count_for_user(db, cur.id),
                settings.quota_projects_count,
            ),
            _item(
                "columns.active",
                await columns_port.count_for_user(db, cur.id),
                settings.quota_columns_count,
            ),
        ]
    )
