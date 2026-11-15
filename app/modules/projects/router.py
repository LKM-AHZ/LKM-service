import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.schemas import (
    MemberRoleUpdate,
    ProjectApplicationCreate,
    ProjectApplicationOut,
    ProjectInviteCreate,
    ProjectInviteOut,
    ProjectJoinRequestCreate,
    ProjectJoinRequestOut,
    ProjectMemberOut,
    ProjectOut,
    ProjectPatch,
    ProjectUpdateCreate,
    ProjectUpdateOut,
    ReviewJoinRequestRequest,
    ReviewProjectApplicationRequest,
)
from app.modules.projects.service import (
    accept_invite,
    add_project_update,
    change_member_role,
    decline_invite,
    get_project_ex,
    invite_member,
    leave_project,
    list_join_requests,
    list_members,
    list_my_applications,
    list_my_invites,
    list_my_join_requests,
    list_project_invites,
    list_project_updates,
    list_projects,
    remove_member,
    review_application,
    review_join_request,
    revoke_invite,
    submit_application,
    submit_join_request,
    update_project,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from app.modules.rbac.service import user_has_permission
from core.common import ApiResp, ModuleStatus
from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.err import BizError, CommonErr, respond
from core.ports import admin_guard as admin_guard_port
from core.ports.authz import get_current_user


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="projects",
        status="implemented",
        responsibility=(
            "项目广场：孵化申请、审核、公开展示；资源级角色（owner/manager/editor/viewer）、"
            "邀请与加入申请、成员治理、项目更新与进展流。"
        ),
        next_steps=[
            "前端项目广场页接线（backlog）",
            "板块挂接 / 积分联动",
        ],
    )


router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/status", response_model=ModuleStatus)
async def projects_status() -> ModuleStatus:
    return _status()


@router.get("", response_model=ApiResp[list[ProjectOut]])
@respond
async def project_list(
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectOut]:
    """项目广场列表（只读）：全部展示型项目，pinned 置顶。"""
    return await list_projects(db)


# ———————— 全局孵化申请（RBAC 权限点保持不变） ————————
@router.get("/applications/me", response_model=ApiResp[list[ProjectApplicationOut]])
@respond
async def my_project_applications(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectApplicationOut]:
    return await list_my_applications(db, cur.id)


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
    _cur: Annotated[CurrentUser, admin_guard_port.require_admin_2fa()],
    db: AsyncSession = Depends(get_session),
) -> ProjectApplicationOut:
    if not await user_has_permission(db, _cur, Permission.projects_application_review):
        raise BizError(CommonErr.FORBIDDEN)
    return await review_application(db, app_id, _cur.id, body)


# ———————— 我的邀请 / 我的加入申请（静态路径先于 /{project_id}） ————————
@router.get("/invites/me", response_model=ApiResp[list[ProjectInviteOut]])
@respond
async def my_invites(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectInviteOut]:
    return await list_my_invites(db, cur.id)


@router.get(
    "/join-requests/me", response_model=ApiResp[list[ProjectJoinRequestOut]]
)
@respond
async def my_join_requests(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectJoinRequestOut]:
    return await list_my_join_requests(db, cur.id)


@router.post(
    "/invites/{invite_id}/accept", response_model=ApiResp[ProjectInviteOut]
)
@respond
async def accept_project_invite(
    invite_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectInviteOut:
    return await accept_invite(db, invite_id, cur.id)


@router.post(
    "/invites/{invite_id}/decline", response_model=ApiResp[ProjectInviteOut]
)
@respond
async def decline_project_invite(
    invite_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectInviteOut:
    return await decline_invite(db, invite_id, cur.id)


@router.post(
    "/join-requests/{request_id}/review",
    response_model=ApiResp[ProjectJoinRequestOut],
)
@respond
async def review_project_join_request(
    request_id: uuid.UUID,
    body: ReviewJoinRequestRequest,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectJoinRequestOut:
    return await review_join_request(db, request_id, cur.id, body)


# ———————— 项目详情 / 更新 ————————
@router.get("/{project_id}", response_model=ApiResp[ProjectOut])
@respond
async def project_detail(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_read_session),
) -> ProjectOut:
    """项目广场详情（只读）：单项目含成员、进展流与旧 reports。"""
    return await get_project_ex(db, project_id)


@router.patch("/{project_id}", response_model=ApiResp[ProjectOut])
@respond
async def patch_project(
    project_id: uuid.UUID,
    body: ProjectPatch,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectOut:
    return await update_project(db, project_id, cur.id, body)


@router.get(
    "/{project_id}/updates", response_model=ApiResp[list[ProjectUpdateOut]]
)
@respond
async def project_updates(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectUpdateOut]:
    return await list_project_updates(db, project_id)


@router.post(
    "/{project_id}/updates", response_model=ApiResp[ProjectUpdateOut]
)
@respond
async def append_project_update(
    project_id: uuid.UUID,
    body: ProjectUpdateCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectUpdateOut:
    return await add_project_update(db, project_id, cur.id, body)


# ———————— 成员 ————————
@router.get(
    "/{project_id}/members", response_model=ApiResp[list[ProjectMemberOut]]
)
@respond
async def project_members(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectMemberOut]:
    return await list_members(db, project_id)


@router.patch(
    "/{project_id}/members/{user_id}", response_model=ApiResp[ProjectMemberOut]
)
@respond
async def patch_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleUpdate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectMemberOut:
    return await change_member_role(db, project_id, cur.id, user_id, body.role)


@router.delete("/{project_id}/members/{user_id}", response_model=ApiResp[None])
@respond
async def delete_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    return await remove_member(db, project_id, cur.id, user_id)


@router.post("/{project_id}/leave", response_model=ApiResp[None])
@respond
async def leave_project_endpoint(
    project_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    return await leave_project(db, project_id, cur.id)


# ———————— 邀请（项目内） ————————
@router.get(
    "/{project_id}/invites", response_model=ApiResp[list[ProjectInviteOut]]
)
@respond
async def project_invites(
    project_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectInviteOut]:
    return await list_project_invites(db, project_id, cur.id)


@router.post("/{project_id}/invites", response_model=ApiResp[ProjectInviteOut])
@respond
async def create_project_invite(
    project_id: uuid.UUID,
    body: ProjectInviteCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectInviteOut:
    return await invite_member(db, project_id, cur.id, body)


@router.post(
    "/{project_id}/invites/{invite_id}/revoke",
    response_model=ApiResp[ProjectInviteOut],
)
@respond
async def revoke_project_invite(
    project_id: uuid.UUID,
    invite_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectInviteOut:
    return await revoke_invite(db, project_id, invite_id, cur.id)


# ———————— 加入申请（项目内） ————————
@router.get(
    "/{project_id}/join-requests",
    response_model=ApiResp[list[ProjectJoinRequestOut]],
)
@respond
async def project_join_requests(
    project_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[ProjectJoinRequestOut]:
    return await list_join_requests(db, project_id, cur.id)


@router.post(
    "/{project_id}/join-requests", response_model=ApiResp[ProjectJoinRequestOut]
)
@respond
async def create_project_join_request(
    project_id: uuid.UUID,
    body: ProjectJoinRequestCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProjectJoinRequestOut:
    return await submit_join_request(db, project_id, cur.id, body)
