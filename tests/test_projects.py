"""projects 模块（行为）：模型建表与审批流、升级、展示服务测试。

拆库(M3.B S5 dual 真 PG)：users/profiles 已迁 auth realm，业务库(Base)无 auth 表。任一建
auth 用户 / 走审批(含 author 升级 auth grant 写、成员 user 存在性快照读) / 业务 HTTP 的用例
均注入 ``auth_db``(+``auth_seam_realm``)：- ``_mk_au(auth_db,…)`` 建 auth realm 用户返回稳定
AuthUser(id)；申请人/成员 account_level·role·token_version 读改于 auth_db；业务
Project/Application/Member 仍落 db。approve 路径的孵化升级经 grant seam 落 auth realm。
"""

import json
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.auth.models import Profile, User
from app.modules.projects.errors import ProjectErr
from app.modules.projects.models import Project, ProjectApplication, ProjectMember
from app.modules.projects.schemas import (
    ProjectApplicationCreate,
    ReviewProjectApplicationRequest,
)
from app.modules.projects.service import (
    get_project,
    list_projects,
    review_application,
    submit_application,
)
from tests.conftest import AuthUser, auth_user_uid

# 不存在的用户 / 项目 id（uuid7 形态），用于校验失败与未命中路径。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


async def _mk_au(
    auth_db: AsyncSession,
    uname: str = "alice",
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


async def _user_level(auth_db: AsyncSession, user_id: uuid.UUID) -> str:
    return str(
        (await auth_db.execute(select(User.account_level).where(User.id == user_id)))
        .scalars()
        .one()
    )


async def _profile_role(auth_db: AsyncSession, user_id: uuid.UUID) -> str | None:
    return (
        await auth_db.execute(select(Profile.role).where(Profile.user_id == user_id))
    ).scalars().first()


async def _token_version(auth_db: AsyncSession, user_id: uuid.UUID) -> int:
    v = (
        await auth_db.execute(select(User.token_version).where(User.id == user_id))
    ).scalars().first()
    return int(v or 0)


async def test_models_can_be_created(db: AsyncSession, auth_db: AsyncSession):
    """三张新表可写可读（冒烟）。"""
    applicant = (await _mk_au(auth_db)).id
    app = ProjectApplication(
        applicant_id=applicant,
        title="LKM",
        summary="s",
        description="d",
        member_claims=json.dumps([{"display_name": "艾尔", "role_in_project": "组长"}]),
        status="pending",
    )
    db.add(app)
    await db.flush()

    proj = Project(
        applicant_id=applicant,
        title="LKM",
        summary="s",
        description="d",
        is_incubated=True,
        status="active",
    )
    db.add(proj)
    await db.flush()
    db.add(
        ProjectMember(
            project_id=proj.id,
            user_id=None,
            display_name="艾尔",
            role_in_project="组长",
            sort_order=0,
        )
    )
    await db.flush()

    rows = (await db.execute(select(ProjectMember))).scalars().all()
    assert len(rows) == 1
    assert rows[0].display_name == "艾尔"


class TestProjectApplicationService:
    async def test_submit_application(self, db: AsyncSession, auth_db: AsyncSession):
        applicant = (await _mk_au(auth_db)).id
        app = await submit_application(
            db,
            applicant,
            ProjectApplicationCreate(
                title="LKM",
                summary="s",
                description="d",
                member_claims=[
                    {"display_name": "艾尔", "role_in_project": "组长", "user_id": None}
                ],
            ),
        )
        assert app.status == "pending"
        assert len(app.member_claims) == 1

    async def test_duplicate_pending_rejected(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        applicant = (await _mk_au(auth_db)).id
        await submit_application(
            db,
            applicant,
            ProjectApplicationCreate(title="a", summary="s", description="d"),
        )
        with pytest.raises(BizError) as e:
            await submit_application(
                db,
                applicant,
                ProjectApplicationCreate(title="a", summary="s", description="d"),
            )
        assert e.value.errcode == ProjectErr.DUPLICATE_APPLICATION

    async def test_review_bad_member_user_rejected(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        """member_claims 引用不存在的 user_id → 申请不落地、报错。"""
        applicant = (await _mk_au(auth_db)).id
        reviewer = (await _mk_au(auth_db, "rv", level="admin")).id
        app = await submit_application(
            db,
            applicant,
            ProjectApplicationCreate(
                title="a",
                summary="s",
                description="d",
                member_claims=[
                    {
                        "display_name": "无人",
                        "role_in_project": "r",
                        "user_id": _MISSING_ID,
                    }
                ],
            ),
        )
        with pytest.raises(BizError) as e:
            await review_application(
                db,
                app.id,
                reviewer,
                ReviewProjectApplicationRequest(approve=True),
            )
        assert e.value.errcode == ProjectErr.MEMBER_USER_NOT_FOUND
        assert await db.scalar(select(Project.id)) is None

    async def test_review_approve_creates_project_and_upgrades(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        applicant = (await _mk_au(auth_db, "ap0")).id  # normal → 升 admin
        reviewer = (await _mk_au(auth_db, "rv", level="admin")).id
        member_uid = (await _mk_au(auth_db, "mem")).id
        app = await submit_application(
            db,
            applicant,
            ProjectApplicationCreate(
                title="LKM",
                summary="s",
                description="d",
                member_claims=[
                    {
                        "display_name": "艾尔",
                        "role_in_project": "组长",
                        "user_id": member_uid,
                    },
                    {
                        "display_name": "外援",
                        "role_in_project": "顾问",
                        "user_id": None,
                    },
                ],
            ),
        )
        out = await review_application(
            db,
            app.id,
            reviewer,
            ReviewProjectApplicationRequest(approve=True, note="ok"),
        )
        assert out.status == "approved"

        proj = await db.scalar(select(Project))
        assert proj is not None
        assert proj.is_incubated is True
        assert proj.status == "active"

        # 申请中的成员落地
        members = (
            (
                await db.execute(
                    select(ProjectMember).where(ProjectMember.project_id == proj.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 2
        by_name = {m.display_name: m for m in members}
        assert by_name["艾尔"].user_id == member_uid
        assert by_name["外援"].user_id is None

        # 申请人升级（auth realm）
        assert await _user_level(auth_db, applicant) == "admin"
        assert await _token_version(auth_db, applicant) >= 1
        assert await _profile_role(auth_db, applicant) == "incubated_member"

    async def test_review_twice_rejected(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        applicant = (await _mk_au(auth_db)).id
        reviewer = (await _mk_au(auth_db, "rv", level="admin")).id
        app = await submit_application(
            db,
            applicant,
            ProjectApplicationCreate(title="a", summary="s", description="d"),
        )
        await review_application(
            db, app.id, reviewer, ReviewProjectApplicationRequest(approve=True)
        )
        with pytest.raises(BizError) as e:
            await review_application(
                db, app.id, reviewer, ReviewProjectApplicationRequest(approve=True)
            )
        assert e.value.errcode == ProjectErr.APPLICATION_ALREADY_REVIEWED

    async def test_review_reject_does_not_create(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        applicant = (await _mk_au(auth_db)).id
        reviewer = (await _mk_au(auth_db, "rv", level="admin")).id
        app = await submit_application(
            db,
            applicant,
            ProjectApplicationCreate(title="a", summary="s", description="d"),
        )
        out = await review_application(
            db,
            app.id,
            reviewer,
            ReviewProjectApplicationRequest(approve=False, note="no"),
        )
        assert out.status == "rejected"
        assert await db.scalar(select(Project.id)) is None
        assert await _user_level(auth_db, applicant) == "normal"  # 未升级

    async def test_upgrade_preserves_higher_role(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        """已有 author 角色的申请人，孵化升级不降其角色。"""
        applicant_au = await _mk_au(auth_db, "a1")
        await auth_db.execute(
            update(Profile)
            .where(Profile.user_id == applicant_au.id)
            .values(role="author")
        )
        await auth_db.flush()
        reviewer = (await _mk_au(auth_db, "rv", level="admin")).id
        app = await submit_application(
            db,
            applicant_au.id,
            ProjectApplicationCreate(title="a", summary="s", description="d"),
        )
        await review_application(
            db, app.id, reviewer, ReviewProjectApplicationRequest(approve=True)
        )
        assert await _profile_role(auth_db, applicant_au.id) == "author"

    async def test_already_admin_does_not_bump_token(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        """申请人已是 admin 且非 member 角色时，孵化通过不应重复递增 token_version。"""
        au = await _mk_au(auth_db, "a2", level="admin", role="author")
        before_token = await _token_version(auth_db, au.id)

        reviewer = (await _mk_au(auth_db, "rv", level="admin")).id
        app = await submit_application(
            db,
            au.id,
            ProjectApplicationCreate(title="a", summary="s", description="d"),
        )
        await review_application(
            db, app.id, reviewer, ReviewProjectApplicationRequest(approve=True)
        )

        assert await _token_version(auth_db, au.id) == before_token  # 无改动则不升
        assert await _user_level(auth_db, au.id) == "admin"  # 仍为 admin，未被降级


async def _make_approved(
    db: AsyncSession, auth_db: AsyncSession, username: str = "alice"
) -> uuid.UUID:
    applicant_au = await _mk_au(auth_db, username)
    reviewer = (await _mk_au(auth_db, f"rv_{username}", level="admin")).id
    app = await submit_application(
        db,
        applicant_au.id,
        ProjectApplicationCreate(
            title="P",
            summary="s",
            description="d",
            member_claims=[
                {"display_name": "艾尔", "role_in_project": "组长", "user_id": None}
            ],
        ),
    )
    await review_application(
        db, app.id, reviewer, ReviewProjectApplicationRequest(approve=True)
    )
    return applicant_au.id


class TestProjectReadService:
    async def test_list_and_detail(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        await _make_approved(db, auth_db)
        projects = await list_projects(db)
        assert len(projects) == 1
        assert projects[0].is_incubated is True
        assert len(projects[0].members) == 1
        detail = await get_project(db, projects[0].id)
        assert detail.title == "P"

    async def test_get_missing_raises(self, db: AsyncSession):
        with pytest.raises(BizError) as e:
            await get_project(db, _MISSING_ID)
        assert e.value.errcode == ProjectErr.PROJECT_NOT_FOUND


class TestProjectRoute:
    async def test_public_list(self, client, db):
        # 只读列表端点已下线，改由 GraphQL projects 承担
        resp = await client.post(
            "/graphql",
            json={
                "query": "query { projects { items { id title } } }",
                "variables": {},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "errors" not in body, body.get("errors")
        assert body["data"]["projects"]["items"] == []

    async def test_submit_requires_auth(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ):
        # 未登录 → 403
        resp = await client.post("/api/v1/projects/applications", json={})
        assert resp.status_code == 403

        # local 用户 → 权限不足（ACCOUNT_LEVEL_INSUFFICIENT）
        loc = await auth_user_uid(
            auth_db,
            username="loc",
            email="loc@e.com",
            nickname="loc",
            account_level="local",
            with_token=True,
        )
        resp = await client.post(
            "/api/v1/projects/applications",
            headers={"Authorization": f"Bearer {loc.token}"},
            json={"title": "t", "summary": "s", "description": "d"},
        )
        assert resp.status_code == 403

        # normal 用户可提交（RBAC 需授 projects.application_create 权限点）
        from app.modules.admin.models import RolePermission

        db.add(
            RolePermission(
                role_name="normal:member",
                permission="projects.application_create",
            )
        )
        await db.flush()
        n_user = await auth_user_uid(
            auth_db,
            username="norm",
            email="norm@e.com",
            nickname="norm",
            account_level="normal",
            with_token=True,
        )
        resp = await client.post(
            "/api/v1/projects/applications",
            headers={"Authorization": f"Bearer {n_user.token}"},
            json={"title": "t", "summary": "s", "description": "d"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "pending"

    async def test_review_requires_admin(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ):
        n_user = await _mk_au(auth_db, "norm", level="normal")
        app = await submit_application(
            db,
            n_user.id,
            ProjectApplicationCreate(title="t", summary="s", description="d"),
        )
        token = await auth_user_uid(
            auth_db,
            username="norm2",
            email="norm2@e.com",
            nickname="norm2",
            account_level="normal",
            with_token=True,
        )
        resp = await client.post(
            f"/api/v1/projects/applications/{app.id}/review",
            headers={"Authorization": f"Bearer {token.token}"},
            json={"approve": True},
        )
        assert resp.status_code == 403
