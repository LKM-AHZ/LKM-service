"""A4 管理面读缝 list_user_snapshots + admin /admin/users PII 门测试。

覆盖：
- 缝缺省(include_pii=False)不泄漏 email/phone；include_pii=True 才带。
- admin /admin/users 列表：默认项 email/phone=None；include_pii=true 项确实带；
  关键字仅筛 username；分页(offset/limit)round-trip(id desc)稳定、total 正确。
- 管理行类型(UserManagementItem)专用于管理授权读，展示缝 UserSnapshot 不受污染。

HTTP 端 auth 设置复用 test_admin_users.py 的既定模式：admin+super_admin+roles 权限点
(<admin/users> 读需 admin_users_manage)。不新增 fixture，复用 conftest db/client。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import RolePermission
from app.modules.rbac.permissions import Permission
from auth.models import Profile, User
from auth.security import hashpwd
from auth.snapshot import (
    UserManagementItem,
    UserSnapshot,
    list_user_snapshots,
)

PERM = Permission.admin_users_manage


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """admin 读 user PII 用例需 auth(user/profile)+biz(role_permissions) 单 schema。"""
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam_for_admin(auth_seam_fused) -> None:
    """/admin/users 端点解析/读 user PII 走 auth seam → fused 里 auth 表。"""


@pytest.fixture(autouse=True)
async def _admin_reader_on_fused(
    fused_db_session: AsyncSession, client: AsyncClient
) -> None:
    """拆库后 ``client`` 把 admin 数据面 auth reader override 到独立 ``auth_db``，而本测
    用户建在 ``fused`` → reader 读不到用户（列表空）。这里把该 reader 也指回 fused，令
    端点内的业务会话与身份读会话同 schema。`client` teardown 会 pop 该键，无需自清。"""
    from app.main import app
    from app.modules.admin.users_router import get_admin_auth_read_session

    async def _override():
        yield fused_db_session

    app.dependency_overrides[get_admin_auth_read_session] = _override


async def _create_user(
    db: AsyncSession,
    username: str,
    account_level: str = "local",
    email: str | None = None,
    phone: str | None = None,
) -> int:
    user = User(
        username=username,
        email=email or f"{username}@example.com",
        phone=phone,
        hashed_password=await hashpwd("secret123456"),
        account_level=account_level,
    )
    db.add(user)
    await db.flush()
    if account_level == "admin":
        db.add(Profile(user_id=user.id, role="super_admin", nickname=username))
    await db.flush()
    return user.id


async def _grant_super_admin(db: AsyncSession) -> None:
    exists = await db.scalar(
        select(RolePermission.id).where(
            RolePermission.role_name == "admin:super_admin",
            RolePermission.permission == PERM.value,
        )
    )
    if exists is None:
        db.add(
            RolePermission(role_name="admin:super_admin", permission=PERM.value)
        )
        await db.flush()


async def _login_admin(
    db: AsyncSession, client: AsyncClient, username: str = "root"
) -> None:
    # S5-A2 后 monolith 不再 serve /admin/auth/login（迁 AUTH 进程）；admin 首见会话 cookie
    # 直接按 fused db(或 auth) 里的 admin 用户 mint，等价该写面产出的会话。
    from app.modules.admin.deps import (
        COOKIE_NAME,
        COOKIE_PATH,
        create_admin_access_token,
    )
    from auth.models import User

    u = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one()
    tok = create_admin_access_token(u)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


# ---------------------------------------------------------------------------
# seam 直连（不需要 HTTP / 权限点）
# ---------------------------------------------------------------------------


async def test_seam_default_no_pii_and_type_is_management(db: AsyncSession) -> None:
    db.add(
        User(
            username="alice",
            email="alice@priv.io",
            phone="13800000000",
            hashed_password=await hashpwd("secret123456"),
            account_level="local",
        )
    )
    await db.flush()
    items, total = await list_user_snapshots(db, q="alice")
    assert total == 1
    item = items[0]
    # 返回的是管理行类型，且是展示缝 UserSnapshot 的孪生非 PII 守卫：
    assert isinstance(item, UserManagementItem)
    assert not isinstance(item, UserSnapshot)
    assert item.username == "alice"
    assert item.email is None
    assert item.phone is None


async def test_seam_include_pii_carries_email_phone(db: AsyncSession) -> None:
    db.add(
        User(
            username="bob",
            email="bob@priv.io",
            phone="13900000000",
            hashed_password=await hashpwd("secret123456"),
        )
    )
    await db.flush()
    items, _ = await list_user_snapshots(db, q="bob", include_pii=True)
    assert items[0].email == "bob@priv.io"
    assert items[0].phone == "13900000000"


async def test_pagination_keyset_stable_by_id_desc(db: AsyncSession) -> None:
    for i in range(7):
        await _create_user(db, f"pg{i}")
    await db.flush()

    collected: list[int] = []
    total = 0
    offset = 0
    limit = 3
    while True:
        page, total = await list_user_snapshots(db, offset=offset, limit=limit)
        ids = [m.id for m in page]
        assert ids == sorted(ids, reverse=True)  # id desc 稳定
        collected += ids
        if len(page) < limit:
            break
        offset += limit
    # round-trip 无重复、无遗漏，与 seed 用户(除已在库的既有)一致
    assert len(collected) == len(set(collected))
    assert total >= 7


async def test_keyword_filters_username_only(db: AsyncSession) -> None:
    await _create_user(db, "zhangsan")
    await _create_user(db, "zhangwei")
    await _create_user(db, "lisi")
    page, total = await list_user_snapshots(db, q="zhang")
    assert total == 2
    assert {m.username for m in page} == {"zhangsan", "zhangwei"}


# ---------------------------------------------------------------------------
# admin /admin/users HTTP（PII 门 + 既有端点契约不破）
# ---------------------------------------------------------------------------


async def test_list_endpoint_hides_pii_by_default(
    db: AsyncSession, client: AsyncClient
) -> None:
    await _create_user(db, "root", account_level="admin")
    await _create_user(db, "carol", account_level="local", email="carol@priv.io")
    await _grant_super_admin(db)
    await _login_admin(db, client, "root")
    resp = await client.get("/api/v1/admin/users")
    assert resp.status_code == 200
    body = resp.json()["data"]
    carol = next(i for i in body["items"] if i["username"] == "carol")
    assert carol["email"] is None
    assert carol["phone"] is None
    # 既有(无 PII)管理字段仍在：created_at/is_locked/account_level 非 PII 恒带
    assert carol["is_locked"] is False
    assert carol["account_level"] == "local"
    assert carol["created_at"] is not None


async def test_list_endpoint_include_pii_true(db: AsyncSession, client: AsyncClient) -> None:
    await _create_user(db, "root", account_level="admin")
    await _create_user(db, "dave", account_level="local", email="dave@priv.io")
    await _grant_super_admin(db)
    await _login_admin(db, client, "root")
    resp = await client.get("/api/v1/admin/users", params={"include_pii": "true"})
    assert resp.status_code == 200
    dave = next(
        i for i in resp.json()["data"]["items"] if i["username"] == "dave"
    )
    assert dave["email"] == "dave@priv.io"


async def test_list_endpoint_keyword_matches_username(
    db: AsyncSession, client: AsyncClient
) -> None:
    await _create_user(db, "root", account_level="admin")
    await _create_user(db, "erin", account_level="local")
    await _grant_super_admin(db)
    await _login_admin(db, client, "root")
    resp = await client.get("/api/v1/admin/users", params={"keyword": "eri"})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["username"] == "erin"
