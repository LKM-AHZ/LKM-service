"""M3.B0.3 + S5-A2 Step2：admin/报表读与 A4 管理读的在线↔离线**边界** 验收。

被测对象是「宽表 read port 是否真读 dim」与「在线管理读是否仍离线隔离、绝不经 dim」这两个
半轴（数据面隔离 B0.3）：

- **边界（管理 read 仍在线）**：把 ``user_dim`` 表**整表 DROP 掉**，再打 A4 实时管理列表
  ``/admin/users`` → 必须仍 200 且返回完整行（证明 A4 list 走 auth 实时缝 + user_dim，绝
  不以 dim 为源；若它偷偷改读 dim，此处必炸 NOT NULL/缺失表）。
- **正测（report read 真读 dim）**：显式 insert 一条 ``UserDim`` 宽表行，经
  ``app.modules.admin.dim_report.list_user_dim`` 读 → 返回该行；``include_pii`` 门控 email
  的带/不带；keyword 过滤与 total 对齐。只读：不产生任何管理/写动作。

S5-A2 Step2：在线 A4 列表的 user 真值在 **auth 库**——admin 经 ``auth_seam_realm`` seam
裁决(auth_db)，reader 经 conftest 覆盖的 auth 只读会话读 auth authoritative；RolePermission
落 biz ``db``。user_dim 离线镜像表仍在 biz realm（B0.2 auth 源 ETL）。报表正测只需造
``UserDim`` 行即可验证 list_user_dim 的读隔离，无需真实 users 行。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.admin.dim_report as dim_report
import auth.models  # noqa: F401  确保 User/Profile 元数据可见（AuthBase 建表）
from app.db.model_registry import ensure_all_models
from app.db.user_dim import UserDim
from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.rbac.permissions import Permission
from auth.models import User
from tests.conftest import auth_user_uid  # type: ignore[attr-defined]


@pytest.fixture
async def _reg() -> AsyncIterator[None]:
    """确保 user_dim 等全量模型注册进 shared Base.metadata（单文件隔离跑也 build dim 表）。"""
    ensure_all_models()
    yield


# 固定 uuid7 形态用户 id（原 42/43/44；跨断言复用）
_UID_A = uuid.UUID("00000000-0000-7000-8000-000000000042")
_UID_B = uuid.UUID("00000000-0000-7000-8000-000000000043")
_UID_C = uuid.UUID("00000000-0000-7000-8000-000000000044")


def _dim_row(
    *,
    user_id: uuid.UUID,
    username: str,
    account_level: str = "local",
    email: str | None = None,
    nickname: str | None = None,
) -> UserDim:
    """离线镜像宽表行（只含非 PII 展示列 + 受门控的 email 镜像 + accounting 时间轴列）。"""
    from app.db.base import now_iso

    created = now_iso()
    return UserDim(
        user_id=user_id,
        username=username,
        email=email,
        nickname=nickname,
        role="member",
        account_level=account_level,
        is_banned=False,
        is_locked=False,
        created_at=created,
        updated_at=created,
        sync_ts=created,
    )


async def mk_auth_admin(
    auth_db: AsyncSession, username: str = "root"
) -> User:
    """在 auth realm 建 admin(super_admin)，返回该 User ORM 供 mint 后台 cookie。"""
    au = await auth_user_uid(
        auth_db,
        username=username,
        account_level="admin",
        role="super_admin",
        with_token=False,
    )
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


async def mk_auth_user(
    auth_db: AsyncSession, username: str, *, email: str | None = None
) -> User:
    au = await auth_user_uid(
        auth_db,
        username=username,
        account_level="local",
        nickname=username,
        email=email,
        with_token=False,
    )
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


def set_admin_cookie(client: AsyncClient, user: User) -> None:
    client.cookies.set(
        COOKIE_NAME,
        create_admin_access_token(user),
        path=COOKIE_PATH,
    )


async def _grant(db: AsyncSession, *perms: Permission) -> None:
    for p in perms:
        exists = await db.scalar(
            select(RolePermission.id).where(
                RolePermission.role_name == "admin:super_admin",
                RolePermission.permission == p.value,
            )
        )
        if exists is None:
            db.add(RolePermission(role_name="admin:super_admin", permission=p.value))
    await db.flush()


class TestReportReadReadsDim:
    """正测：离线报表 read port 确实读 user_dim（读侧接通 B0.2 填充的数据）。"""

    async def test_list_user_dim_returns_inserted_row(
        self, db: AsyncSession, _reg: None
    ) -> None:
        db.add(
            _dim_row(
                user_id=_UID_A,
                username="repuser",
                email="repuser@example.com",
                nickname="报表客",
            )
        )
        await db.commit()
        rows, total = await dim_report.list_user_dim(db, include_pii=True)
        assert total >= 1
        hit = next(r for r in rows if r.username == "repuser")
        assert hit.user_id == _UID_A
        assert hit.email == "repuser@example.com"  # PII: include_pii=True 才带
        assert hit.nickname == "报表客"
        assert hit.account_level == "local"
        assert hit.is_banned is False
        assert hit.sync_ts is not None

    async def test_email_hidden_without_pii_gate(
        self, db: AsyncSession, _reg: None
    ) -> None:
        db.add(
            _dim_row(
                user_id=_UID_B,
                username="repuser",
                email="repuser@example.com",
                nickname="报表客",
            )
        )
        await db.commit()
        rows, _ = await dim_report.list_user_dim(db, include_pii=False)
        hit = next(r for r in rows if r.username == "repuser")
        assert hit.email is None

    async def test_keyword_filters_and_counts(
        self, db: AsyncSession, _reg: None
    ) -> None:
        db.add(_dim_row(user_id=_UID_C, username="repuser"))
        await db.commit()
        rows, total = await dim_report.list_user_dim(db, q="repuse", include_pii=False)
        assert total >= 1
        assert all("repuse" in r.username for r in rows)


class TestAdminManagementStaysOfflineIsolated:
    """边界：A4 实时管理列表读**绝不可**以 user_dim 为源（在报表隔离下仍实时、经 auth 缝）。"""

    async def test_admin_users_list_survives_dropped_dim_table(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await mk_auth_admin(auth_db, "root")
        await mk_auth_user(auth_db, "alice")
        await _grant(db, Permission.admin_users_manage, Permission.admin_dashboard)
        set_admin_cookie(client, root)

        # drop 掉整个离线宽表：任何经 dim 的读此刻都会抛 → 若 A4 列表碰 dim 则此处崩
        await db.execute(text("DROP TABLE IF EXISTS user_dim"))
        await db.commit()

        resp = await client.get("/api/v1/admin/users")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["total"] >= 2  # root + alice（在线实时源 auth realm，非 dim）
        assert any(i["username"] == "alice" for i in body["items"])

    async def test_management_list_pii_gate_untouched_by_dim(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await mk_auth_admin(auth_db, "root")
        await mk_auth_user(auth_db, "alice", email="alice@example.com")
        await _grant(db, Permission.admin_users_manage, Permission.admin_dashboard)
        set_admin_cookie(client, root)

        resp = await client.get(
            "/api/v1/admin/users", params={"include_pii": "true"}
        )
        assert resp.status_code == 200
        alice = next(i for i in resp.json()["data"]["items"] if i["username"] == "alice")
        assert alice["email"] == "alice@example.com"
