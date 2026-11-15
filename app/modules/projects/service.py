"""项目广场服务：孵化申请、审核、公开列表以及资源级协作（角色/邀请/加入申请/进展）。

权限口径集中在 :func:`_require_project_role`：owner > manager > editor > viewer，
所有项目内写操作都必须先过这一道，路由层只负责鉴权（谁在调用），不再重复判角色。
"""

import json
import uuid

from sqlalchemy.exc import IntegrityError

from app.modules.projects.errors import ProjectErr
from app.modules.projects.models import (
    Project,
    ProjectApplication,
    ProjectInvite,
    ProjectJoinRequest,
    ProjectMember,
)
from app.modules.projects.repository import (
    ProjectApplicationRepository,
    ProjectInviteRepository,
    ProjectJoinRequestRepository,
    ProjectMemberRepository,
    ProjectRepository,
    ProjectUpdateRepository,
)
from app.modules.projects.schemas import (
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
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError
from core.ports import notification as notification_port
from core.ports.audit import record_audit
from core.ports.snapshot import get_user_display_names as _applicant_names
from core.ports.snapshot import get_user_snapshot_batch

#: 角色层级：所有写操作按「角色 ≥ 某级」判定。
ROLE_RANK: dict[str, int] = {
    "viewer": 1,
    "editor": 2,
    "manager": 3,
    "owner": 4,
}

#: role_in_project 的展示标签（历史字段保留，不参与鉴权）。
ROLE_LABELS: dict[str, str] = {
    "owner": "负责人",
    "manager": "管理员",
    "editor": "编辑",
    "viewer": "成员",
}


def _app_to_schema(a: ProjectApplication) -> ProjectApplicationOut:
    return ProjectApplicationOut(
        id=a.id,
        applicant_id=a.applicant_id,
        title=a.title,
        summary=a.summary,
        description=a.description,
        status=a.status,
        member_claims=json.loads(a.member_claims or "[]"),
        reviewer_id=a.reviewer_id,
        review_note=a.review_note,
        created_at=a.created_at,
        reviewed_at=a.reviewed_at,
    )


def _project_to_schema(p: Project, *, applicant_name: str) -> ProjectOut:
    out = ProjectOut.model_validate(p)
    out.members = [ProjectMemberOut.model_validate(m) for m in p.members]
    out.updates = [ProjectUpdateOut.model_validate(u) for u in p.updates]
    out.applicant_name = applicant_name
    return out


async def _display_name(db: DbSession, user_id: uuid.UUID) -> str:
    names = await _applicant_names(db, [user_id])
    return names.get(user_id) or str(user_id)


async def _notify(
    db: DbSession,
    *,
    user_id: uuid.UUID,
    type_name: str,
    payload: dict,
    actor_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
) -> None:
    await notification_port.create(
        db,
        user_id=user_id,
        type=notification_port.type_value(type_name),
        payload=payload,
        actor_id=actor_id,
        target_id=target_id,
    )


async def _get_project_or_raise(db: DbSession, project_id: uuid.UUID) -> Project:
    project = await ProjectRepository(db).get(project_id)
    if project is None:
        raise BizError(ProjectErr.PROJECT_NOT_FOUND)
    return project


async def _ensure_owner_member(db: DbSession, project: Project) -> ProjectMember:
    """申请人即 owner：create_all 老数据/手工建项目时惰性补齐 owner 成员行。"""
    repo = ProjectMemberRepository(db)
    member = await repo.get_any(project.id, project.applicant_id)
    if member is None:
        member = await repo.create(
            project_id=project.id,
            user_id=project.applicant_id,
            display_name=await _display_name(db, project.applicant_id),
            role_in_project=ROLE_LABELS["owner"],
            sort_order=-1,
            role="owner",
            status="active",
            joined_at=now_iso(),
        )
    elif member.status != "active" or member.role != "owner":
        member.status = "active"
        member.role = "owner"
        member.joined_at = now_iso()
        await repo.flush()
    return member


async def _require_project_role(
    db: DbSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    min_role: str,
) -> ProjectMember:
    """对象级权限唯一入口：返回调用者的活跃成员行。

    成员行是权威（转让后原申请人可能已被降级）；仅在没有成员行的老数据上，
    把项目申请人按 owner 补齐。
    """
    project = await _get_project_or_raise(db, project_id)
    member = await ProjectMemberRepository(db).get_active(project_id, user_id)
    if member is None:
        if project.applicant_id != user_id:
            raise BizError(ProjectErr.FORBIDDEN)
        member = await _ensure_owner_member(db, project)
    if ROLE_RANK.get(member.role, 0) < ROLE_RANK[min_role]:
        raise BizError(ProjectErr.FORBIDDEN)
    return member


async def _upsert_member(
    db: DbSession,
    *,
    project: Project,
    user_id: uuid.UUID,
    role: str,
    invited_by: uuid.UUID | None,
) -> ProjectMember:
    """接受邀请/申请通过时落成员行；已退出（left）的旧行原地复活。"""
    if role not in ROLE_RANK:
        raise BizError(ProjectErr.INVALID_ROLE)
    repo = ProjectMemberRepository(db)
    existing = await repo.get_any(project.id, user_id)
    if existing is not None:
        if existing.status == "active":
            raise BizError(ProjectErr.ALREADY_MEMBER)
        existing.status = "active"
        existing.role = role
        existing.role_in_project = ROLE_LABELS.get(role, role)
        existing.joined_at = now_iso()
        existing.invited_by = invited_by
        await repo.flush()
        return existing
    return await repo.create(
        project_id=project.id,
        user_id=user_id,
        display_name=await _display_name(db, user_id),
        role_in_project=ROLE_LABELS.get(role, role),
        sort_order=(await repo.max_sort_order(project.id)) + 1,
        role=role,
        status="active",
        joined_at=now_iso(),
        invited_by=invited_by,
    )


# ———————— 孵化申请（全局 RBAC，保持原有语义） ————————
async def submit_application(
    db: DbSession, applicant_id: uuid.UUID, info: ProjectApplicationCreate
) -> ProjectApplicationOut:
    title = info.title.strip()
    if await ProjectApplicationRepository(db).pending_duplicate_exists(
        applicant_id=applicant_id, title=title
    ):
        raise BizError(ProjectErr.DUPLICATE_APPLICATION)
    try:
        # savepoint 包住插入：并发下撞部分唯一索引 uq_project_applications_pending 时
        # 只回滚这一条插入（不污染调用方事务），再翻译成业务错误码
        async with db.begin_nested():
            app_ = await ProjectApplicationRepository(db).create(
                applicant_id=applicant_id,
                title=title,
                summary=info.summary,
                description=info.description,
                member_claims=json.dumps(
                    [m.model_dump(mode="json") for m in info.member_claims],
                    ensure_ascii=False,
                ),
                status="pending",
            )
    except IntegrityError:
        raise BizError(ProjectErr.DUPLICATE_APPLICATION) from None
    return _app_to_schema(app_)


async def _assert_member_users_exist(db: DbSession, user_ids: set[uuid.UUID]) -> None:
    """给定的 member user_ids 必须是存在的用户。

    只在“通过审核”时（review_application 的 approve 路径）校验：提交阶段不拦截，
    待审申请可引用尚未注册的受邀账号，审核时再决定。遵循任务测试契约
    test_review_bad_member_user_rejected：提交不报错、审核时才报 MEMBER_USER_NOT_FOUND。
    """
    if not user_ids:
        return
    # 身份存在性走 auth 快照缝（business 不直读 auth.users；拆库后存在性由 auth 权威）。
    present = await get_user_snapshot_batch(db, user_ids=sorted(user_ids))
    missing = user_ids - set(present)
    if missing:
        raise BizError(ProjectErr.MEMBER_USER_NOT_FOUND)


async def review_application(
    db: DbSession,
    application_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    body: ReviewProjectApplicationRequest,
) -> ProjectApplicationOut:
    app_ = await ProjectApplicationRepository(db).get_or_raise(
        application_id, ProjectErr.APPLICATION_NOT_FOUND
    )
    repo = ProjectApplicationRepository(db)
    if app_.status != "pending":
        raise BizError(ProjectErr.APPLICATION_ALREADY_REVIEWED)

    # 通过审核前先校验贡献成员 user 均存在；若不存在则提前报错、申请保持 pending
    # （与 boards.review_application 在修改状态前核对 slug 冲突的做法一致）。
    if body.approve:
        claims = json.loads(app_.member_claims or "[]")
        await _assert_member_users_exist(
            db,
            {uuid.UUID(c["user_id"]) for c in claims if c.get("user_id")},
        )
    else:
        claims = []

    status = "approved" if body.approve else "rejected"
    claimed = await repo.update_where(
        {
            "reviewer_id": reviewer_id,
            "review_note": body.note,
            "reviewed_at": now_iso(),
            "status": status,
        },
        ProjectApplication.id == application_id,
        ProjectApplication.status == "pending",
    )
    if not claimed:
        raise BizError(ProjectErr.APPLICATION_ALREADY_REVIEWED)
    await db.refresh(app_)  # Core UPDATE 后用同一事务重读，保证返回体是新状态

    if body.approve:
        # 落 Project
        project = await ProjectRepository(db).create(
            applicant_id=app_.applicant_id,
            title=app_.title,
            summary=app_.summary,
            description=app_.description,
            is_incubated=True,
            status="active",
        )
        # 申请人即 owner：先落 owner 行，再落申请中的贡献成员（默认 viewer）。
        # 同一 user_id 只落一行：活跃成员的部分唯一索引会拒绝重复成员行。
        await _ensure_owner_member(db, project)
        members: list[ProjectMember] = []
        seen_users: set[uuid.UUID] = set()
        for i, c in enumerate(claims):
            uid = (
                uuid.UUID(c["user_id"])
                if isinstance(c.get("user_id"), str)
                else None
            )
            if uid == app_.applicant_id or (uid is not None and uid in seen_users):
                continue
            if uid is not None:
                seen_users.add(uid)
            members.append(
                ProjectMember(
                    project_id=project.id,
                    user_id=uid,
                    display_name=str(c.get("display_name", "")),
                    role_in_project=str(c.get("role_in_project", "")),
                    sort_order=i,
                    role="viewer",
                    status="active",
                    joined_at=now_iso(),
                )
            )
        member_repo = ProjectMemberRepository(db)
        await member_repo.add_all(members)
        await member_repo.flush()
        await _apply_incubation(db, app_.applicant_id)

    await record_audit(
        db,
        reviewer_id,
        "review_project_application",
        detail=json.dumps(
            {
                "target_type": "project_application",
                "target_id": str(app_.id),
                "applicant_id": str(app_.applicant_id),
                "decision": status,
                "reason": body.note,
                "previous_status": "pending",
                "at": now_iso().isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    return _app_to_schema(app_)


async def _apply_incubation(db: DbSession, applicant_id: uuid.UUID) -> None:
    """纳入成员升级：account_level→admin / member role→incubated_member（auth 域权威写面）。

    业务 realm 已无 users/profiles——auth 是身份词表唯一 owner（含写）。升权恒经
    ``auth.seams.grant_incubation_from_business`` 打 auth 内部写端点落地 auth realm
    （测试用 ``auth_seam_realm`` 替身），不再有本地同库执行的回退分支。
    """
    from core.ports.users import grant_incubation_from_business

    await grant_incubation_from_business(db, applicant_id)


# ———————— 读 ————————
async def list_projects(db: DbSession) -> list[ProjectOut]:
    rows = await ProjectRepository(db).list_active_with_members()
    names = await _applicant_names(db, [p.applicant_id for p in rows])
    return [
        _project_to_schema(p, applicant_name=names.get(p.applicant_id, ""))
        for p in rows
    ]


async def list_my_applications(
    db: DbSession, applicant_id: uuid.UUID
) -> list[ProjectApplicationOut]:
    rows = await ProjectApplicationRepository(db).list_for_applicant(applicant_id)
    return [_app_to_schema(row) for row in rows]


async def get_project(db: DbSession, project_id: uuid.UUID) -> Project:
    return await ProjectRepository(db).get_with_members_or_raise(
        project_id, ProjectErr.PROJECT_NOT_FOUND
    )


async def get_project_ex(db: DbSession, project_id: uuid.UUID) -> ProjectOut:
    p = await get_project(db, project_id)
    names = await _applicant_names(db, [p.applicant_id])
    return _project_to_schema(p, applicant_name=names.get(p.applicant_id, ""))


async def list_members(db: DbSession, project_id: uuid.UUID) -> list[ProjectMemberOut]:
    await _get_project_or_raise(db, project_id)
    rows = await ProjectMemberRepository(db).list_active(project_id)
    return [ProjectMemberOut.model_validate(m) for m in rows]


# ———————— 项目写入（owner/manager） ————————
async def update_project(
    db: DbSession, project_id: uuid.UUID, actor_id: uuid.UUID, body: ProjectPatch
) -> ProjectOut:
    await _require_project_role(db, project_id, actor_id, "manager")
    project = await get_project(db, project_id)
    data = body.model_dump(exclude_unset=True)
    if data:
        await ProjectRepository(db).update(project, **data)
    names = await _applicant_names(db, [project.applicant_id])
    return _project_to_schema(project, applicant_name=names.get(project.applicant_id, ""))


# ———————— 成员治理 ————————
async def change_member_role(
    db: DbSession,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    new_role: str,
) -> ProjectMemberOut:
    """仅 owner 可改角色；把成员升为 owner 即转让（原 owner 降为 manager）。"""
    actor = await _require_project_role(db, project_id, actor_id, "owner")
    if new_role not in ROLE_RANK:
        raise BizError(ProjectErr.INVALID_ROLE)
    repo = ProjectMemberRepository(db)
    target = await repo.get_active(project_id, user_id)
    if target is None:
        raise BizError(ProjectErr.MEMBER_NOT_FOUND)
    if new_role == "owner":
        if target.id == actor.id:
            return ProjectMemberOut.model_validate(target)
        actor.role = "manager"
        actor.role_in_project = ROLE_LABELS["manager"]
        target.role = "owner"
        target.role_in_project = ROLE_LABELS["owner"]
    else:
        target.role = new_role
        target.role_in_project = ROLE_LABELS[new_role]
    await repo.flush()
    return ProjectMemberOut.model_validate(target)


async def remove_member(
    db: DbSession,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """owner/manager 可移除成员，但 owner 不可被移除。"""
    await _require_project_role(db, project_id, actor_id, "manager")
    repo = ProjectMemberRepository(db)
    target = await repo.get_active(project_id, user_id)
    if target is None:
        raise BizError(ProjectErr.MEMBER_NOT_FOUND)
    if target.role == "owner":
        raise BizError(ProjectErr.CANNOT_REMOVE_OWNER)
    target.status = "left"
    await repo.flush()
    project = await _get_project_or_raise(db, project_id)
    await _notify(
        db,
        user_id=user_id,
        type_name="PROJECT_MEMBER_REMOVED",
        payload={"project_id": str(project_id), "project_title": project.title},
        actor_id=actor_id,
        target_id=project_id,
    )


async def leave_project(
    db: DbSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    member = await _require_project_role(db, project_id, user_id, "viewer")
    if member.role == "owner":
        raise BizError(ProjectErr.OWNER_CANNOT_LEAVE)
    member.status = "left"
    await ProjectMemberRepository(db).flush()


# ———————— 邀请 ————————
async def invite_member(
    db: DbSession,
    project_id: uuid.UUID,
    inviter_id: uuid.UUID,
    body: ProjectInviteCreate,
) -> ProjectInviteOut:
    await _require_project_role(db, project_id, inviter_id, "manager")
    project = await _get_project_or_raise(db, project_id)
    if await ProjectMemberRepository(db).get_active(project_id, body.user_id):
        raise BizError(ProjectErr.ALREADY_MEMBER)
    if not await get_user_snapshot_batch(db, user_ids=[body.user_id]):
        raise BizError(ProjectErr.MEMBER_USER_NOT_FOUND)
    repo = ProjectInviteRepository(db)
    if await repo.get_pending(project_id, body.user_id):
        raise BizError(ProjectErr.DUPLICATE_INVITE)
    try:
        async with db.begin_nested():
            invite = await repo.create(
                project_id=project_id,
                user_id=body.user_id,
                role=body.role,
                status="invited",
                invited_by=inviter_id,
            )
    except IntegrityError:
        raise BizError(ProjectErr.DUPLICATE_INVITE) from None
    await _notify(
        db,
        user_id=body.user_id,
        type_name="PROJECT_INVITED",
        payload={
            "project_id": str(project_id),
            "project_title": project.title,
            "role": body.role,
        },
        actor_id=inviter_id,
        target_id=project_id,
    )
    return ProjectInviteOut.model_validate(invite)


async def list_project_invites(
    db: DbSession, project_id: uuid.UUID, actor_id: uuid.UUID
) -> list[ProjectInviteOut]:
    await _require_project_role(db, project_id, actor_id, "manager")
    rows = await ProjectInviteRepository(db).list_for_project(project_id)
    return [ProjectInviteOut.model_validate(i) for i in rows]


async def list_my_invites(
    db: DbSession, user_id: uuid.UUID
) -> list[ProjectInviteOut]:
    rows = await ProjectInviteRepository(db).list_for_user(user_id)
    return [ProjectInviteOut.model_validate(i) for i in rows]


async def _get_invite_or_raise(
    db: DbSession, invite_id: uuid.UUID
) -> ProjectInvite:
    invite = await ProjectInviteRepository(db).get(invite_id)
    if invite is None:
        raise BizError(ProjectErr.INVITE_NOT_FOUND)
    return invite


async def _claim_invite(
    db: DbSession, invite: ProjectInvite, status: str
) -> ProjectInvite:
    updated = await ProjectInviteRepository(db).update_where(
        {"status": status, "updated_at": now_iso()},
        ProjectInvite.id == invite.id,
        ProjectInvite.status == "invited",
    )
    if not updated:
        raise BizError(ProjectErr.INVITE_NOT_PENDING)
    await db.refresh(invite)
    return invite


async def accept_invite(
    db: DbSession, invite_id: uuid.UUID, user_id: uuid.UUID
) -> ProjectInviteOut:
    invite = await _get_invite_or_raise(db, invite_id)
    if invite.user_id != user_id:
        raise BizError(ProjectErr.FORBIDDEN)
    if invite.status != "invited":
        raise BizError(ProjectErr.INVITE_NOT_PENDING)
    project = await _get_project_or_raise(db, invite.project_id)
    if await ProjectMemberRepository(db).get_active(project.id, user_id):
        raise BizError(ProjectErr.ALREADY_MEMBER)
    invite = await _claim_invite(db, invite, "accepted")
    await _upsert_member(
        db,
        project=project,
        user_id=user_id,
        role=invite.role,
        invited_by=invite.invited_by,
    )
    return ProjectInviteOut.model_validate(invite)


async def decline_invite(
    db: DbSession, invite_id: uuid.UUID, user_id: uuid.UUID
) -> ProjectInviteOut:
    invite = await _get_invite_or_raise(db, invite_id)
    if invite.user_id != user_id:
        raise BizError(ProjectErr.FORBIDDEN)
    if invite.status != "invited":
        raise BizError(ProjectErr.INVITE_NOT_PENDING)
    invite = await _claim_invite(db, invite, "declined")
    return ProjectInviteOut.model_validate(invite)


async def revoke_invite(
    db: DbSession, project_id: uuid.UUID, invite_id: uuid.UUID, actor_id: uuid.UUID
) -> ProjectInviteOut:
    await _require_project_role(db, project_id, actor_id, "manager")
    invite = await _get_invite_or_raise(db, invite_id)
    if invite.project_id != project_id:
        raise BizError(ProjectErr.INVITE_NOT_FOUND)
    invite = await _claim_invite(db, invite, "revoked")
    return ProjectInviteOut.model_validate(invite)


# ———————— 加入申请（与孵化申请分离） ————————
async def submit_join_request(
    db: DbSession,
    project_id: uuid.UUID,
    applicant_id: uuid.UUID,
    body: ProjectJoinRequestCreate,
) -> ProjectJoinRequestOut:
    await _get_project_or_raise(db, project_id)
    if await ProjectMemberRepository(db).get_active(project_id, applicant_id):
        raise BizError(ProjectErr.ALREADY_MEMBER)
    repo = ProjectJoinRequestRepository(db)
    if await repo.get_pending(project_id, applicant_id):
        raise BizError(ProjectErr.DUPLICATE_JOIN_REQUEST)
    try:
        async with db.begin_nested():
            request = await repo.create(
                project_id=project_id,
                applicant_id=applicant_id,
                role_requested=body.role_requested,
                message=body.message,
                status="pending",
            )
    except IntegrityError:
        raise BizError(ProjectErr.DUPLICATE_JOIN_REQUEST) from None
    return ProjectJoinRequestOut.model_validate(request)


async def list_join_requests(
    db: DbSession, project_id: uuid.UUID, actor_id: uuid.UUID
) -> list[ProjectJoinRequestOut]:
    await _require_project_role(db, project_id, actor_id, "manager")
    rows = await ProjectJoinRequestRepository(db).list_for_project(project_id)
    return [ProjectJoinRequestOut.model_validate(r) for r in rows]


async def list_my_join_requests(
    db: DbSession, applicant_id: uuid.UUID
) -> list[ProjectJoinRequestOut]:
    rows = await ProjectJoinRequestRepository(db).list_for_applicant(applicant_id)
    return [ProjectJoinRequestOut.model_validate(r) for r in rows]


async def review_join_request(
    db: DbSession,
    request_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    body: ReviewJoinRequestRequest,
) -> ProjectJoinRequestOut:
    request = await ProjectJoinRequestRepository(db).get(request_id)
    if request is None:
        raise BizError(ProjectErr.JOIN_REQUEST_NOT_FOUND)
    await _require_project_role(
        db, request.project_id, reviewer_id, "manager"
    )
    if request.status != "pending":
        raise BizError(ProjectErr.JOIN_REQUEST_ALREADY_REVIEWED)
    status = "approved" if body.approve else "rejected"
    updated = await ProjectJoinRequestRepository(db).update_where(
        {
            "status": status,
            "reviewer_id": reviewer_id,
            "review_note": body.note,
            "reviewed_at": now_iso(),
        },
        ProjectJoinRequest.id == request_id,
        ProjectJoinRequest.status == "pending",
    )
    if not updated:
        raise BizError(ProjectErr.JOIN_REQUEST_ALREADY_REVIEWED)
    await db.refresh(request)

    project = await _get_project_or_raise(db, request.project_id)
    if body.approve:
        await _upsert_member(
            db,
            project=project,
            user_id=request.applicant_id,
            role=request.role_requested,
            invited_by=None,
        )
    await _notify(
        db,
        user_id=request.applicant_id,
        type_name=(
            "PROJECT_JOIN_APPROVED" if body.approve else "PROJECT_JOIN_REJECTED"
        ),
        payload={
            "project_id": str(project.id),
            "project_title": project.title,
            "role": request.role_requested,
            "note": body.note,
        },
        actor_id=reviewer_id,
        target_id=project.id,
    )
    return ProjectJoinRequestOut.model_validate(request)


# ———————— 进展 / 报告追加（editor+） ————————
async def add_project_update(
    db: DbSession,
    project_id: uuid.UUID,
    author_id: uuid.UUID,
    body: ProjectUpdateCreate,
) -> ProjectUpdateOut:
    await _require_project_role(db, project_id, author_id, "editor")
    update = await ProjectUpdateRepository(db).create(
        project_id=project_id,
        author_id=author_id,
        kind=body.kind,
        title=body.title,
        content=body.content,
        progress=body.progress,
    )
    return ProjectUpdateOut.model_validate(update)


async def list_project_updates(
    db: DbSession, project_id: uuid.UUID
) -> list[ProjectUpdateOut]:
    await _get_project_or_raise(db, project_id)
    rows = await ProjectUpdateRepository(db).list_for_project(project_id)
    return [ProjectUpdateOut.model_validate(u) for u in rows]
