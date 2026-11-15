"""项目协作：资源级角色、邀请、加入申请、成员治理、进展流与通知。

覆盖验收口径：角色层级、邀请接受/拒绝、加入申请审核、owner 不可移除、
editor 只能追加更新、通知落库、并发重复申请。业务库（db）与 auth 独立库（auth_db）
按拆库形态注入；跨 realm 身份读走 ``auth_seam_realm`` 替身。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notification.models import Notification
from app.modules.notification.service import NotificationType
from app.modules.projects.errors import ProjectErr
from app.modules.projects.models import (
    Project,
    ProjectInvite,
    ProjectJoinRequest,
    ProjectMember,
    ProjectUpdate,
)
from app.modules.projects.repository import ProjectJoinRequestRepository
from app.modules.projects.schemas import (
    ProjectApplicationCreate,
    ProjectInviteCreate,
    ProjectJoinRequestCreate,
    ProjectPatch,
    ProjectUpdateCreate,
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
    list_project_updates,
    remove_member,
    review_application,
    review_join_request,
    submit_application,
    submit_join_request,
    update_project,
)
from core.err import BizError
from tests.conftest import AuthUser, auth_user_uid


async def _mk_au(
    auth_db: AsyncSession,
    uname: str,
    level: str = "normal",
    role: str = "member",
) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=uname,
        email=f"{uname}@e.com",
        nickname=uname,
        account_level=level,
        role=role,
        with_token=False,
    )


async def _make_project(
    db: AsyncSession, auth_db: AsyncSession, name: str = "owner"
) -> tuple[Project, uuid.UUID]:
    """走孵化审核通过路径建项目：申请人成为 owner，owner 成员行随之落库。"""
    applicant = await _mk_au(auth_db, name)
    reviewer = await _mk_au(auth_db, f"rv_{name}", level="admin")
    app = await submit_application(
        db,
        applicant.id,
        ProjectApplicationCreate(title=name, summary="s", description="d"),
    )
    await review_application(
        db, app.id, reviewer.id, ReviewProjectApplicationRequest(approve=True)
    )
    project = await db.scalar(
        select(Project).where(Project.applicant_id == applicant.id)
    )
    assert project is not None
    return project, applicant.id


async def _add_member(
    db: AsyncSession,
    project: Project,
    owner_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> None:
    invite = await invite_member(
        db, project.id, owner_id, ProjectInviteCreate(user_id=user_id, role=role)
    )
    await accept_invite(db, invite.id, user_id)


async def _notifications(
    db: AsyncSession, user_id: uuid.UUID, type_name: str
) -> list[Notification]:
    value = NotificationType[type_name].value
    return list(
        (
            await db.execute(
                select(Notification).where(
                    Notification.user_id == user_id, Notification.type == value
                )
            )
        )
        .scalars()
        .all()
    )


class TestOwnerBootstrap:
    async def test_owner_member_row_created_on_approval(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        member = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == owner_id,
            )
        )
        assert member is not None
        assert member.role == "owner"
        assert member.status == "active"

    async def test_role_hierarchy_sort(self) -> None:
        from app.modules.projects.service import ROLE_RANK

        assert (
            ROLE_RANK["owner"]
            > ROLE_RANK["manager"]
            > ROLE_RANK["editor"]
            > ROLE_RANK["viewer"]
        )


class TestInvites:
    async def test_accept_invite_creates_member_and_notifies(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        invitee = await _mk_au(auth_db, "invitee")

        invite = await invite_member(
            db,
            project.id,
            owner_id,
            ProjectInviteCreate(user_id=invitee.id, role="editor"),
        )
        assert invite.status == "invited"
        assert invite.role == "editor"
        notes = await _notifications(db, invitee.id, "PROJECT_INVITED")
        assert len(notes) == 1
        assert notes[0].actor_id == owner_id
        assert notes[0].target_id == project.id

        accepted = await accept_invite(db, invite.id, invitee.id)
        assert accepted.status == "accepted"
        member = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == invitee.id,
            )
        )
        assert member is not None
        assert member.role == "editor"
        assert member.invited_by == owner_id

        with pytest.raises(BizError) as exc:
            await accept_invite(db, invite.id, invitee.id)
        assert exc.value.errcode == ProjectErr.INVITE_NOT_PENDING

    async def test_decline_invite_leaves_no_member(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        invitee = await _mk_au(auth_db, "decliner")
        invite = await invite_member(
            db,
            project.id,
            owner_id,
            ProjectInviteCreate(user_id=invitee.id),
        )
        declined = await decline_invite(db, invite.id, invitee.id)
        assert declined.status == "declined"
        members = await list_members(db, project.id)
        assert {m.user_id for m in members} == {owner_id}

    async def test_duplicate_pending_invite_rejected(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        invitee = await _mk_au(auth_db, "dup")
        await invite_member(
            db, project.id, owner_id, ProjectInviteCreate(user_id=invitee.id)
        )
        with pytest.raises(BizError) as exc:
            await invite_member(
                db, project.id, owner_id, ProjectInviteCreate(user_id=invitee.id)
            )
        assert exc.value.errcode == ProjectErr.DUPLICATE_INVITE

    async def test_editor_cannot_invite(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        editor = await _mk_au(auth_db, "editor0")
        await _add_member(db, project, owner_id, editor.id, "editor")
        target = await _mk_au(auth_db, "target0")
        with pytest.raises(BizError) as exc:
            await invite_member(
                db,
                project.id,
                editor.id,
                ProjectInviteCreate(user_id=target.id),
            )
        assert exc.value.errcode == ProjectErr.FORBIDDEN


class TestJoinRequests:
    async def test_approve_join_request(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        applicant = await _mk_au(auth_db, "joiner")
        request = await submit_join_request(
            db,
            project.id,
            applicant.id,
            ProjectJoinRequestCreate(role_requested="editor", message="hi"),
        )
        assert request.status == "pending"

        out = await review_join_request(
            db,
            request.id,
            owner_id,
            ReviewJoinRequestRequest(approve=True, note="ok"),
        )
        assert out.status == "approved"
        member = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == applicant.id,
            )
        )
        assert member is not None
        assert member.role == "editor"
        notes = await _notifications(db, applicant.id, "PROJECT_JOIN_APPROVED")
        assert len(notes) == 1

    async def test_reject_join_request(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        applicant = await _mk_au(auth_db, "rejectee")
        request = await submit_join_request(
            db,
            project.id,
            applicant.id,
            ProjectJoinRequestCreate(),
        )
        out = await review_join_request(
            db,
            request.id,
            owner_id,
            ReviewJoinRequestRequest(approve=False, note="no"),
        )
        assert out.status == "rejected"
        assert out.review_note == "no"
        member = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == applicant.id,
            )
        )
        assert member is None
        notes = await _notifications(db, applicant.id, "PROJECT_JOIN_REJECTED")
        assert len(notes) == 1

    async def test_duplicate_pending_join_request(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, _owner_id = await _make_project(db, auth_db)
        applicant = await _mk_au(auth_db, "dupjoin")
        await submit_join_request(
            db, project.id, applicant.id, ProjectJoinRequestCreate()
        )
        with pytest.raises(BizError) as exc:
            await submit_join_request(
                db, project.id, applicant.id, ProjectJoinRequestCreate()
            )
        assert exc.value.errcode == ProjectErr.DUPLICATE_JOIN_REQUEST

    async def test_concurrent_duplicate_hits_unique_index(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """跳过先查后拒的预检，验证部分唯一索引竞态被翻译成同一业务错误码。"""
        project, _owner_id = await _make_project(db, auth_db)
        applicant = await _mk_au(auth_db, "racejoin")
        await submit_join_request(
            db, project.id, applicant.id, ProjectJoinRequestCreate()
        )

        async def _no_pending(self, *_a, **_k):
            return None

        monkeypatch.setattr(ProjectJoinRequestRepository, "get_pending", _no_pending)
        with pytest.raises(BizError) as exc:
            await submit_join_request(
                db, project.id, applicant.id, ProjectJoinRequestCreate()
            )
        assert exc.value.errcode == ProjectErr.DUPLICATE_JOIN_REQUEST
        rows = (
            await db.execute(
                select(ProjectJoinRequest).where(
                    ProjectJoinRequest.project_id == project.id,
                    ProjectJoinRequest.applicant_id == applicant.id,
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_non_manager_cannot_list_requests(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        editor = await _mk_au(auth_db, "edjoin")
        await _add_member(db, project, owner_id, editor.id, "editor")
        with pytest.raises(BizError) as exc:
            await list_join_requests(db, project.id, editor.id)
        assert exc.value.errcode == ProjectErr.FORBIDDEN


class TestMemberGovernance:
    async def test_owner_cannot_be_removed_or_leave(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        with pytest.raises(BizError) as exc:
            await remove_member(db, project.id, owner_id, owner_id)
        assert exc.value.errcode == ProjectErr.CANNOT_REMOVE_OWNER
        with pytest.raises(BizError) as exc2:
            await leave_project(db, project.id, owner_id)
        assert exc2.value.errcode == ProjectErr.OWNER_CANNOT_LEAVE

    async def test_manager_removes_member_and_notifies(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        manager = await _mk_au(auth_db, "mgr")
        editor = await _mk_au(auth_db, "ed1")
        await _add_member(db, project, owner_id, manager.id, "manager")
        await _add_member(db, project, owner_id, editor.id, "editor")

        await remove_member(db, project.id, manager.id, editor.id)
        member = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == editor.id,
            )
        )
        assert member is not None
        assert member.status == "left"
        notes = await _notifications(db, editor.id, "PROJECT_MEMBER_REMOVED")
        assert len(notes) == 1
        with pytest.raises(BizError) as exc:
            await add_project_update(
                db,
                project.id,
                editor.id,
                ProjectUpdateCreate(content="removed editor"),
            )
        assert exc.value.errcode == ProjectErr.FORBIDDEN

    async def test_manager_cannot_remove_owner(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        manager = await _mk_au(auth_db, "mgr2")
        await _add_member(db, project, owner_id, manager.id, "manager")
        with pytest.raises(BizError) as exc:
            await remove_member(db, project.id, manager.id, owner_id)
        assert exc.value.errcode == ProjectErr.CANNOT_REMOVE_OWNER

    async def test_only_owner_changes_roles_and_transfer(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        manager = await _mk_au(auth_db, "mgr3")
        viewer = await _mk_au(auth_db, "vw3")
        await _add_member(db, project, owner_id, manager.id, "manager")
        await _add_member(db, project, owner_id, viewer.id, "viewer")

        with pytest.raises(BizError) as exc:
            await change_member_role(db, project.id, manager.id, viewer.id, "editor")
        assert exc.value.errcode == ProjectErr.FORBIDDEN

        changed = await change_member_role(
            db, project.id, owner_id, viewer.id, "editor"
        )
        assert changed.role == "editor"

        # 升 manager 为 owner = 转让；原 owner 降为 manager。
        promoted = await change_member_role(
            db, project.id, owner_id, manager.id, "owner"
        )
        assert promoted.role == "owner"
        old_owner = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == owner_id,
            )
        )
        assert old_owner is not None
        assert old_owner.role == "manager"
        # 降级后的原 owner 不能再改角色。
        with pytest.raises(BizError) as exc2:
            await change_member_role(db, project.id, owner_id, viewer.id, "viewer")
        assert exc2.value.errcode == ProjectErr.FORBIDDEN


class TestProjectWrites:
    async def test_manager_updates_project(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        manager = await _mk_au(auth_db, "mgrw")
        await _add_member(db, project, owner_id, manager.id, "manager")
        out = await update_project(
            db,
            project.id,
            manager.id,
            ProjectPatch(title="New", progress=42, is_recruiting=True),
        )
        assert out.title == "New"
        assert out.progress == 42
        assert out.is_recruiting is True

    async def test_editor_cannot_patch_but_can_append_updates(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        editor = await _mk_au(auth_db, "edw")
        await _add_member(db, project, owner_id, editor.id, "editor")

        with pytest.raises(BizError) as exc:
            await update_project(
                db, project.id, editor.id, ProjectPatch(title="nope")
            )
        assert exc.value.errcode == ProjectErr.FORBIDDEN

        out = await add_project_update(
            db,
            project.id,
            editor.id,
            ProjectUpdateCreate(kind="progress", title="v1", content="done", progress=10),
        )
        assert out.kind == "progress"
        assert out.progress == 10

    async def test_viewer_cannot_append_updates(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        viewer = await _mk_au(auth_db, "vw4")
        await _add_member(db, project, owner_id, viewer.id, "viewer")
        with pytest.raises(BizError) as exc:
            await add_project_update(
                db, project.id, viewer.id, ProjectUpdateCreate(content="x")
            )
        assert exc.value.errcode == ProjectErr.FORBIDDEN

    async def test_updates_history_visible_in_project_out(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        await add_project_update(
            db, project.id, owner_id, ProjectUpdateCreate(kind="report", content="a")
        )
        await add_project_update(
            db, project.id, owner_id, ProjectUpdateCreate(kind="progress", content="b")
        )
        rows = await list_project_updates(db, project.id)
        assert [r.content for r in rows] == ["a", "b"]
        out = await get_project_ex(db, project.id)
        assert [u.content for u in out.updates] == ["a", "b"]
        # 旧 reports JSON 保留，不被新表改写。
        assert out.reports == []

    async def test_update_requires_known_project(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        user = await _mk_au(auth_db, "ghost")
        with pytest.raises(BizError) as exc:
            await add_project_update(
                db,
                uuid.UUID("00000000-0000-7000-8000-000000000999"),
                user.id,
                ProjectUpdateCreate(content="x"),
            )
        assert exc.value.errcode == ProjectErr.PROJECT_NOT_FOUND


class TestRoutes:
    async def test_members_route_and_write_requires_auth(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        resp = await client.get(f"/api/v1/projects/{project.id}/members")
        assert resp.status_code == 200
        members = resp.json()["data"]
        assert len(members) == 1
        assert members[0]["user_id"] == str(owner_id)
        assert members[0]["role"] == "owner"

        resp = await client.patch(
            f"/api/v1/projects/{project.id}", json={"title": "nope"}
        )
        assert resp.status_code == 403


class TestCollaborationPersistence:
    async def test_join_request_and_update_rows_persist(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        project, owner_id = await _make_project(db, auth_db)
        applicant = await _mk_au(auth_db, "persist")
        await submit_join_request(
            db, project.id, applicant.id, ProjectJoinRequestCreate(message="m")
        )
        await add_project_update(
            db, project.id, owner_id, ProjectUpdateCreate(content="c")
        )
        assert await db.scalar(select(ProjectJoinRequest.id)) is not None
        assert await db.scalar(select(ProjectUpdate.id)) is not None
        assert await db.scalar(select(ProjectInvite.id)) is None
