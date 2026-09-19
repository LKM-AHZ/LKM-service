"""后台审校规则管理：/admin/moderation/rules 增删改查。

读列表走 ``require_admin``；写（增/改/删/test）属危险操作，走 ``require_admin_2fa``
（与 admin 其它写端点的 step-up 一致），并在其上叠加 ``require_permission``
``admin.moderation_manage`` 细粒度权限点——规则暴露内部正则权重，仅授该点的
管理员（super_admin）可读可写。写后经 service 层 bump 规则缓存版本。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp, ListData
from app.core.err import respond
from app.db.session import get_session
from app.modules.admin.deps import require_admin, require_admin_2fa
from app.modules.admin.moderation import service as mod_service
from app.modules.admin.moderation.schemas import (
    RuleCreate,
    RuleInfo,
    RuleTestRequest,
    RuleTestResult,
    RuleUpdate,
)
from app.modules.admin.permissions import require_permission
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser

router = APIRouter(prefix="/admin/moderation", tags=["admin-moderation"])


@router.get("/rules", response_model=ApiResp[ListData[RuleInfo]])
@respond
async def admin_list_moderation_rules(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> dict[str, list[RuleInfo]]:
    # 规则暴露内部正则权重，仅授予该权限点的管理员可读
    await require_permission(db, cur, Permission.admin_moderation_manage)
    return {"items": await mod_service.list_rules(db)}


@router.post("/rules", response_model=ApiResp[RuleInfo])
@respond
async def admin_create_moderation_rule(
    info: RuleCreate,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> RuleInfo:
    await require_permission(db, cur, Permission.admin_moderation_manage)
    return await mod_service.create_rule(db, info)


@router.patch("/rules/{rule_id}", response_model=ApiResp[RuleInfo])
@respond
async def admin_update_moderation_rule(
    rule_id: int,
    info: RuleUpdate,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> RuleInfo:
    await require_permission(db, cur, Permission.admin_moderation_manage)
    return await mod_service.update_rule(db, rule_id, info)


@router.delete("/rules/{rule_id}", response_model=ApiResp[dict[str, bool]])
@respond
async def admin_delete_moderation_rule(
    rule_id: int,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await require_permission(db, cur, Permission.admin_moderation_manage)
    await mod_service.delete_rule(db, rule_id)
    return {"ok": True}


@router.post("/rules/test", response_model=ApiResp[RuleTestResult])
@respond
async def admin_test_moderation_rules(
    req: RuleTestRequest,
    cur: CurrentUser = require_admin_2fa,
    db: AsyncSession = Depends(get_session),
) -> RuleTestResult:
    await require_permission(db, cur, Permission.admin_moderation_manage)
    return await mod_service.test_rules(db, req.text)
