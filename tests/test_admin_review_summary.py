"""``GET /admin/review-summary``：五队列聚合、最旧等待时长、权限门禁、空队列。"""

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import Report, RolePermission
from app.modules.content.models import BoardApplication, ColumnApplication
from app.modules.files.models import LibraryFile
from app.modules.projects.models import ProjectApplication
from auth.models import Profile, User
from core.db.base import now_iso
from tests.conftest import DB, Client


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """admin + 五队列表同库（融合装配）。"""
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam(auth_seam_fused: None) -> None:
    """require_admin 经 seam 裁决到本测 auth 表。"""


async def _mk_admin(
    db: DB, username: str = "summary_admin", role: str = "super_admin"
) -> User:
    user = User(
        username=username,
        account_level="admin",
        hashed_password="summary-placeholder-not-real",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role=role))
    await db.flush()
    return user


async def _grant(db: DB, role: str, *perms: str) -> None:
    for perm in perms:
        db.add(RolePermission(role_name=role, permission=perm))
    await db.flush()


def _set_cookie(client: Client, user: User) -> None:
    token = create_admin_access_token(user)
    client.cookies.set(COOKIE_NAME, token, path=COOKIE_PATH)


async def _seed_queues(db: DB) -> uuid.UUID:
    uid = uuid.uuid4()
    db.add(
        LibraryFile(
            uploader_id=uid,
            original_name="pending.pdf",
            stored_name="summary-pending.pdf",
            status="pending",
            created_at=now_iso() - dt.timedelta(hours=2),
        )
    )
    db.add(
        Report(
            type="post",
            target_id="t-1",
            status="pending",
            created_at=now_iso() - dt.timedelta(minutes=30),
        )
    )
    db.add(
        BoardApplication(
            applicant_id=uid,
            title="板块申请",
            description="d",
            reason="r",
            slug="summary-board",
            status="pending",
        )
    )
    db.add(
        ColumnApplication(
            user_id=uid, title="专栏申请", description="d", reason="r", status="pending"
        )
    )
    db.add(
        ProjectApplication(
            applicant_id=uid,
            title="项目申请",
            summary="s",
            description="d",
            status="pending",
        )
    )
    # auth 域队列：待审账号（fused 库里的 auth.users）
    db.add(
        User(
            username="pending_admission",
            hashed_password="pending-placeholder",
            account_level="normal",
            admission_status="pending",
            applied_at=now_iso() - dt.timedelta(minutes=5),
        )
    )
    await db.flush()
    return uid


async def test_summary_counts_oldest_and_links(db: DB, client: Client) -> None:
    await _grant(db, "admin:super_admin", "admin.dashboard")
    admin = await _mk_admin(db)
    await _seed_queues(db)
    _set_cookie(client, admin)

    resp = await client.get("/api/v1/admin/review-summary")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_pending"] == 6
    queues = {item["key"]: item for item in data["queues"]}
    assert set(queues) == {
        "files",
        "reports",
        "board_applications",
        "column_applications",
        "project_applications",
        "admission_users",
    }
    assert queues["files"]["count"] == 1
    assert queues["files"]["oldest_waiting_seconds"] >= 7_000
    assert queues["files"]["href"] == "/admin/files"
    assert queues["reports"]["href"] == "/admin/reports"
    # 三类申请暂无后台审核页：不返回深链，避免前端 404
    assert queues["board_applications"]["href"] is None
    assert queues["column_applications"]["href"] is None
    assert queues["project_applications"]["href"] is None
    # 待审账号有独立审核入口（用户管理页）
    assert queues["admission_users"]["count"] == 1
    assert queues["admission_users"]["href"] == "/admin/users"
    assert queues["admission_users"]["oldest_waiting_seconds"] is None
    for item in data["queues"]:
        assert item["count"] == 1


async def test_empty_queues_report_zero(db: DB, client: Client) -> None:
    await _grant(db, "admin:super_admin", "admin.dashboard")
    admin = await _mk_admin(db)
    _set_cookie(client, admin)

    resp = await client.get("/api/v1/admin/review-summary")

    data = resp.json()["data"]
    assert data["total_pending"] == 0
    assert all(item["count"] == 0 for item in data["queues"])
    assert all(item["oldest_waiting_seconds"] is None for item in data["queues"])


async def test_requires_dashboard_permission(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)  # 未授 admin.dashboard
    _set_cookie(client, admin)

    resp = await client.get("/api/v1/admin/review-summary")

    assert resp.status_code == 403
