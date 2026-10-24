"""后台待办审核聚合端点：``GET /admin/review-summary``。

纯读聚合（聚合口径在 ``review_service.build_review_summary``，与每日摘要共用）：
dashboards 卡片按队列展示 count + 最旧等待时长 + 审核页深链。

全部数据都在业务库（users 不参与聚合），故单 ``get_read_session`` 即可，不做跨库 join。
三类申请目前没有后台审核页，``href=None``——前端只展示数字，不链到 404。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.review_service import build_review_summary
from app.modules.admin.schemas import AdminReviewSummary
from app.modules.rbac.permissions import Permission
from core.common import ApiResp
from core.contracts import CurrentUser
from core.db.session import get_read_session
from core.err import respond

from .deps import require_admin
from .permissions import require_permission

router = APIRouter(prefix="/admin", tags=["admin-review"])


@router.get("/review-summary", response_model=ApiResp[AdminReviewSummary])
@respond
async def admin_review_summary(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_read_session),
) -> AdminReviewSummary:
    """待办审核聚合：各队列 count + 最旧等待时长（秒）+ 深链。

    权限口径与 ``/admin/stats`` 一致（``admin.dashboard``）：这是聚合计数、非内容正文，
    dashboard 角色可见；具体审核动作仍由各域 review 权限点另行裁决。
    """
    await require_permission(db, cur, Permission.admin_dashboard)
    return await build_review_summary(db)
