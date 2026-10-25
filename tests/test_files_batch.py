"""文件批量审核：限流、逐条独立事务/部分成功、逐条审计、驳回必填原因、权限+2FA。"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.files.models import LibraryFile
from auth.models import AuditLog, Profile, User
from core.err import CommonErr
from tests.conftest import DB, Client


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam(auth_seam_fused: None) -> None:
    """require_admin_2fa 与审核审计都经 seam 落到本测 auth 表。"""


async def _mk_admin(
    db: DB, username: str = "batch_admin", *, grant: bool = True
) -> User:
    user = User(
        username=username,
        account_level="admin",
        hashed_password="batch-admin-placeholder",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="super_admin"))
    if grant:
        db.add(RolePermission(role_name="admin:super_admin", permission="files.review"))
    await db.flush()
    return user


async def _mk_uploader(db: DB) -> User:
    user = User(
        username="batch_uploader",
        email="batch_uploader@example.com",
        hashed_password="uploader-placeholder",
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _mk_file(db: DB, uploader: User, name: str) -> LibraryFile:
    row = LibraryFile(
        uploader_id=uploader.id,
        original_name=name,
        stored_name=name,
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


def _cookie(client: Client, user: User, *, mfa: bool = True) -> None:
    token = create_admin_access_token(user, mfa_verified=mfa)
    client.cookies.set(COOKIE_NAME, token, path=COOKIE_PATH)


async def test_batch_approve_partial_success(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)
    uploader = await _mk_uploader(db)
    first = await _mk_file(db, uploader, "batch-1.pdf")
    second = await _mk_file(db, uploader, "batch-2.pdf")
    # 请求内逐条 commit 会让本测会话的 ORM 对象到期；先取纯 id，避免事后懒加载。
    first_id, second_id = first.id, second.id
    missing = uuid.uuid4()
    _cookie(client, admin)

    resp = await client.post(
        "/api/v1/files/review-batch",
        json={
            "file_ids": [str(first_id), str(missing), str(second_id)],
            "status": "approved",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    by_id = {row["file_id"]: row for row in data["results"]}
    assert by_id[str(first_id)]["ok"] is True
    assert by_id[str(missing)]["ok"] is False
    assert by_id[str(second_id)]["ok"] is True

    await db.refresh(first)
    await db.refresh(second)
    assert first.status == "approved"
    assert second.status == "approved"
    audits = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "review_file")))
        .scalars()
        .all()
    )
    assert len(audits) == 2  # 成功后已提交，失败条目不回滚已成功条目


async def test_batch_reject_requires_reason(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)
    uploader = await _mk_uploader(db)
    row = await _mk_file(db, uploader, "batch-reject.pdf")
    _cookie(client, admin)

    resp = await client.post(
        "/api/v1/files/review-batch",
        json={"file_ids": [str(row.id)], "status": "rejected", "review_comment": "  "},
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == CommonErr.INVALID_INPUT


async def test_batch_reject_with_reason(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)
    uploader = await _mk_uploader(db)
    row = await _mk_file(db, uploader, "batch-reject-ok.pdf")
    _cookie(client, admin)

    resp = await client.post(
        "/api/v1/files/review-batch",
        json={
            "file_ids": [str(row.id)],
            "status": "rejected",
            "review_comment": "版权不明",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["succeeded"] == 1
    await db.refresh(row)
    assert row.status == "rejected"
    assert row.review_comment == "版权不明"


async def test_batch_limit_and_duplicate_ids(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)
    uploader = await _mk_uploader(db)
    row = await _mk_file(db, uploader, "batch-limit.pdf")
    _cookie(client, admin)

    too_many = await client.post(
        "/api/v1/files/review-batch",
        json={"file_ids": [str(uuid.uuid4()) for _ in range(51)], "status": "approved"},
    )
    assert too_many.status_code == 422

    duplicated = await client.post(
        "/api/v1/files/review-batch",
        json={
            "file_ids": [str(row.id), str(row.id)],
            "status": "approved",
        },
    )
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["succeeded"] == 1
    assert len(duplicated.json()["data"]["results"]) == 1


async def test_batch_requires_permission_and_2fa(db: DB, client: Client) -> None:
    # 无 files.review 权限：403（即使有 2FA 信任）
    admin = await _mk_admin(db, grant=False)
    uploader = await _mk_uploader(db)
    row = await _mk_file(db, uploader, "batch-perm.pdf")
    _cookie(client, admin)
    denied = await client.post(
        "/api/v1/files/review-batch",
        json={"file_ids": [str(row.id)], "status": "approved"},
    )
    assert denied.status_code == 403

    # 有权限但无 2FA 信任：401 code=4
    db.add(RolePermission(role_name="admin:super_admin", permission="files.review"))
    await db.flush()
    _cookie(client, admin, mfa=False)
    no_mfa = await client.post(
        "/api/v1/files/review-batch",
        json={"file_ids": [str(row.id)], "status": "approved"},
    )
    assert no_mfa.status_code == 401
    assert no_mfa.json()["code"] == CommonErr.MFA_REQUIRED
