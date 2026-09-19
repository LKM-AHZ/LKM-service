"""项目广场服务：孵化申请、审核（通过→建项目+落成员+纳入成员升级）、公开展示。"""

import json
import uuid

from app.core.err import BizError
from app.db.base import now_iso
from app.db.repository import DbSession
from app.modules.auth.snapshot import get_user_snapshot_batch
from app.modules.projects.errors import ProjectErr
from app.modules.projects.models import Project, ProjectApplication, ProjectMember
from app.modules.projects.repository import (
    ProjectApplicationRepository,
    ProjectMemberRepository,
    ProjectRepository,
)
from app.modules.projects.schemas import (
    ProjectApplicationCreate,
    ProjectApplicationOut,
    ProjectMemberOut,
    ProjectOut,
    ReviewProjectApplicationRequest,
)


def _app_to_schema(a: ProjectApplication) -> ProjectApplicationOut:
    # member_claims 在 DB 是 JSON 文本列，需显式解析（同 exam.QuestionOut.from_model 的
    # options 坑）：不能用 model_validate 自动转换，会把字符串塞进 list 报错。
    # 显式构造，先解析 JSON 再喂给 schema 的 list[dict] 字段。
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
    out.applicant_name = applicant_name
    return out


async def _applicant_names(
    db: DbSession, applicant_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """批量取申请人展示名（seam 口径 = nickname or username）；缺失 id 不在结果里。"""
    if not applicant_ids:
        return {}
    snaps = await get_user_snapshot_batch(
        db, user_ids=list(dict.fromkeys(applicant_ids))
    )
    return {uid: s.display_name for uid, s in snaps.items()}


async def submit_application(
    db: DbSession, applicant_id: uuid.UUID, info: ProjectApplicationCreate
) -> ProjectApplicationOut:
    # 同一申请人同名的 pending 申请唯一性（防重复刷单）
    if await ProjectApplicationRepository(db).pending_duplicate_exists(
        applicant_id=applicant_id, title=info.title
    ):
        raise BizError(ProjectErr.DUPLICATE_APPLICATION)
    app_ = await ProjectApplicationRepository(db).create(
        applicant_id=applicant_id,
        title=info.title,
        summary=info.summary,
        description=info.description,
        member_claims=json.dumps(
            [m.model_dump(mode="json") for m in info.member_claims],
            ensure_ascii=False,
        ),
        status="pending",
    )
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

    app_.reviewer_id = reviewer_id
    app_.review_note = body.note
    app_.reviewed_at = now_iso()
    app_.status = "approved" if body.approve else "rejected"
    await ProjectApplicationRepository(db).flush()

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
        # 落成员（含非注册成员：user_id=None）
        members = [
            ProjectMember(
                project_id=project.id,
                user_id=(
                    uuid.UUID(c["user_id"])
                    if isinstance(c.get("user_id"), str)
                    else None
                ),
                display_name=str(c.get("display_name", "")),
                role_in_project=str(c.get("role_in_project", "")),
                sort_order=i,
            )
            for i, c in enumerate(claims)
        ]
        member_repo = ProjectMemberRepository(db)
        await member_repo.add_all(members)
        await _apply_incubation(db, app_.applicant_id)
        await member_repo.flush()
    return _app_to_schema(app_)


async def _apply_incubation(db: DbSession, applicant_id: uuid.UUID) -> None:
    """纳入成员升级：account_level→admin / member role→incubated_member（auth 域权威写面）。

    M3.B S5 C：拆库后本项目 DB 会话（业务 realm）已无 users/profiles——auth 是身份词表唯一
    owner（含写）。故不再把业务 ``db`` 直塞 auth 的 grant 例程；改经 seam 调度
    ``service_authz.grant_incubation_from_business(db, …)``：seam 开时（生产拆库 + 测试
    auth_seam_realm）由 auth 内部写端点把升权落地 auth realm；seam 关时回落本地同库会话执行
    （蓝绿/单库，语义与旧实现一一对等并发出 user.updated）。
    """
    from app.modules.auth import service_authz

    await service_authz.grant_incubation_from_business(db, applicant_id)


async def list_projects(db: DbSession) -> list[ProjectOut]:
    rows = await ProjectRepository(db).list_active_with_members()
    names = await _applicant_names(db, [p.applicant_id for p in rows])
    return [
        _project_to_schema(p, applicant_name=names.get(p.applicant_id, ""))
        for p in rows
    ]


async def get_project(db: DbSession, project_id: uuid.UUID) -> Project:
    return await ProjectRepository(db).get_with_members_or_raise(
        project_id, ProjectErr.PROJECT_NOT_FOUND
    )


async def get_project_ex(db: DbSession, project_id: uuid.UUID) -> ProjectOut:
    p = await get_project(db, project_id)
    names = await _applicant_names(db, [p.applicant_id])
    return _project_to_schema(p, applicant_name=names.get(p.applicant_id, ""))
