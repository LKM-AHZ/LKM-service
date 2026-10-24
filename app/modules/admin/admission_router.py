"""后台准入审核（P2 申请制注册）：``/admin/admission/*``。

- 列表/决策都经 ``core.ports.users`` 交 auth 权威执行（users 在 auth 独立库）；
- 权限点 ``admin.users_review``；决策额外要求后台 2FA step-up 信任；
- 审计与会话撤销在 auth 侧 ``decide_admission`` 内完成（audit_logs 属 auth 库）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.schemas import AdmissionDecisionIn, AdmissionUserItem
from app.modules.rbac.permissions import Permission
from core.common import ApiResp
from core.contracts import CurrentUser
from core.db.session import get_session
from core.err import BizError, CommonErr, respond
from core.ports.users import decide_admission, list_admission_users

from .deps import require_admin, require_admin_2fa
from .permissions import require_permission

router = APIRouter(prefix="/admin/admission", tags=["admin-admission"])


@router.get("/users", response_model=ApiResp[list[AdmissionUserItem]])
@respond
async def admission_user_list(
    status: str = Query(default="pending"),
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> list[AdmissionUserItem]:
    """按准入状态列用户（默认 pending），供后台审核页。"""
    await require_permission(db, cur, Permission.admin_users_review)
    rows = await list_admission_users(db, status)
    return [AdmissionUserItem.model_validate(row) for row in rows]


@router.post("/users/{user_id}/decision", response_model=ApiResp[AdmissionUserItem])
@respond
async def admission_user_decide(
    user_id: uuid.UUID,
    body: AdmissionDecisionIn,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> AdmissionUserItem:
    """approve/reject 一个待审账号；reject 必须填原因。"""
    await require_permission(db, cur, Permission.admin_users_review)
    if body.action == "reject" and not (body.reason or "").strip():
        raise BizError(CommonErr.INVALID_INPUT, "拒绝必须填写原因")
    row = await decide_admission(
        db,
        user_id=user_id,
        action=body.action,
        reviewer_id=cur.id,
        reason=body.reason,
    )
    return AdmissionUserItem.model_validate(row)
