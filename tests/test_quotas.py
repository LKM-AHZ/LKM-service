"""统一配额端点 ``GET /api/v1/quotas/me``。

覆盖：未登录 401、三类资源计数、已配置上限/剩余量、未配置上限返回 null。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.columns.models import Column
from app.modules.files.models import FileStatus, LibraryFile
from app.modules.projects.models import Project, ProjectMember
from core.config import settings
from tests.conftest import AuthUser, Client, auth_user_uid


async def _mk_au(auth_db: AsyncSession, uname: str = "qz") -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=uname,
        nickname=uname,
        account_level="normal",
        role="member",
    )


def _h(au: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {au.token}"}


async def test_quota_requires_login(client: Client) -> None:
    r = await client.get("/api/v1/quotas/me")
    assert r.status_code == 401


async def test_quota_counts_and_limits(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "quota_files_count", 1)
    monkeypatch.setattr(settings, "quota_projects_count", None)
    monkeypatch.setattr(settings, "quota_columns_count", 5)

    user = await _mk_au(auth_db, "quota_user")
    other = await _mk_au(auth_db, "quota_other")

    # files.count：本人未删除 2 个，已删除 1 个不计，他人 1 个不计
    db.add_all(
        [
            LibraryFile(
                uploader_id=user.id,
                original_name=f"a{i}.txt",
                stored_name=f"a{i}",
                status=FileStatus.APPROVED,
            )
            for i in range(2)
        ]
    )
    db.add(
        LibraryFile(
            uploader_id=user.id,
            original_name="gone.txt",
            stored_name="gone",
            status=FileStatus.DELETED,
        )
    )
    db.add(
        LibraryFile(
            uploader_id=other.id,
            original_name="other.txt",
            stored_name="other",
            status=FileStatus.APPROVED,
        )
    )

    # projects.active：本人申请的 1 个 + 作为成员的 1 个；无关项目不计
    own = Project(
        title="own", summary="s", description="d", applicant_id=user.id
    )
    joined = Project(
        title="joined", summary="s", description="d", applicant_id=other.id
    )
    unrelated = Project(
        title="unrelated", summary="s", description="d", applicant_id=other.id
    )
    db.add_all([own, joined, unrelated])
    await db.flush()
    db.add(
        ProjectMember(
            project_id=joined.id,
            user_id=user.id,
            display_name="u",
            role_in_project="member",
            role="editor",
        )
    )

    # columns.active：本人 1 个，他人 1 个不计
    db.add(Column(owner_id=user.id, title="mine", description="d"))
    db.add(Column(owner_id=other.id, title="theirs", description="d"))
    await db.flush()

    r = await client.get("/api/v1/quotas/me", headers=_h(user))
    assert r.status_code == 200
    items = {item["resource"]: item for item in r.json()["data"]["items"]}
    assert items["files.count"] == {
        "resource": "files.count",
        "used": 2,
        "limit": 1,
        "remaining": 0,  # 超额夹到 0
    }
    assert items["projects.active"] == {
        "resource": "projects.active",
        "used": 2,
        "limit": None,
        "remaining": None,
    }
    assert items["columns.active"] == {
        "resource": "columns.active",
        "used": 1,
        "limit": 5,
        "remaining": 4,
    }
