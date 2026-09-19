import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import bump_collection_version
from app.core.common import ApiResp, ModuleStatus
from app.core.err import BizError, CommonErr, respond
from app.db.session import get_session
from app.modules.admin.deps import require_admin_2fa
from app.modules.projects.schemas import (
    ProjectApplicationCreate,
    ProjectApplicationOut,
    ProjectOut,
    ReviewProjectApplicationRequest,
)
from app.modules.projects.service import (
    get_project_ex,
    list_projects,
    review_application,
    submit_application,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission, composible_role
from app.modules.rbac.service import role_has_permission
from auth.deps import CurrentUser


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="projects",
        status="implemented",
        responsibility="项目广场：孵化申请、审核、通过后展示项目与孵化标识、纳入成员升级、贡献者展示。",
        next_steps=[
            "前端项目广场页接线（backlog）",
            "板块挂接 / 积分联动 / 多成员协作",
        ],
    )


router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/status", response_model=ModuleStatus)
async def projects_status() -> ModuleStatus:
    return _status()


@router.get("", response_model=ApiResp[list[ProjectOut]])
@respond
async def project_list(
    db: AsyncSession = Depends(get_session),
) -> list[ProjectOut]:
    """项目广场列表（只读）：全部展示型项目，pinned 置顶。"""
    return await list_projects(db)


@router.get("/{project_id}", response_model=ApiResp[ProjectOut])
@respond
async def project_detail(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> ProjectOut:
    """项目广场详情（只读）：单项目含成员与进展报告。"""
    return await get_project_ex(db, project_id)


@router.post("/applications", response_model=ApiResp[ProjectApplicationOut])
@respond
async def submit_app(
    info: ProjectApplicationCreate,
    cur: CurrentUser = RequirePermission(Permission.projects_application_create),
    db: AsyncSession = Depends(get_session),
) -> ProjectApplicationOut:
    return await submit_application(db, cur.id, info)


@router.post(
    "/applications/{app_id}/review", response_model=ApiResp[ProjectApplicationOut]
)
@respond
async def review_app(
    app_id: uuid.UUID,
    body: ReviewProjectApplicationRequest,
    _cur: Annotated[CurrentUser, require_admin_2fa],
    db: AsyncSession = Depends(get_session),
) -> ProjectApplicationOut:
    # require_admin_2fa 已保证 admin 会话 + 2FA 信任；此处再叠加 projects_application_review
    # 权限点（super_admin 有，org_member 无）。校验失败按 FORBIDDEN 返回。
    role = composible_role(_cur.account_level, _cur.role)
    if not await role_has_permission(db, role, Permission.projects_application_review):
        raise BizError(CommonErr.FORBIDDEN)
    result = await review_application(db, app_id, _cur.id, body)
    await bump_collection_version("projects")
    return result
