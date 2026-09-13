"""M3.B S2 auth internal 授权/升权/验密 seams 验收。

- 端点 fail-closed：auth_http_token 未配（默认）或错/缺 Bearer → grant/verify-password 均 401，
  此内部写缝不成为公网面。
- 配齐内部 token 后：/auth/internal/grant 按 kind 做 auth 侧单向升权（changed 语义），
  verify-password 校验凭据。升权会 bump token_version（令旧令牌失效）。
- auth 侧的授权原语（grant_exam_unlock/grant_incubation）直接以 service 层覆盖单向提升与幂等。
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.auth.router_authz  # noqa: F401  # 确保 ROUTERS 已装好 router_authz
from app.core.config import settings
from app.db.auth_session import get_auth_session
from app.main import app as _app
from app.modules.auth.models import Profile, User
from app.modules.auth.security import hashpwd
from app.modules.auth.service_authz import grant_exam_unlock
from tests.conftest import DB, Client


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """auth 内部 seam 用 auth 独立库（User/Profile/token_version 等 auth 表）。"""
    return auth_db


@pytest.fixture(autouse=True)
async def _bind_internal_auth_session(db: DB):
    """内部端点 S5 拆库后走 get_auth_session（auth 库），覆盖到本测 auth schema。"""
    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db

    _app.dependency_overrides[get_auth_session] = _override
    try:
        yield
    finally:
        _app.dependency_overrides.pop(get_auth_session, None)


@pytest.fixture(autouse=True)
def _reset_settings_token():
    """每用例后清零 auth_http_token，避免跨用例污染内部缝开关。"""
    yield
    settings.auth_http_token = ""


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.auth_http_token}"}


async def _mk_user(
    db: AsyncSession,
    username: str,
    *,
    account_level: str = "normal",
    role: str = "member",
) -> int:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secretwxyz123"),
        account_level=account_level,
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role=role))
    await db.flush()
    return user.id


async def _level(db: AsyncSession, uid: int) -> tuple[str, str, int]:
    row = (
        await db.execute(
            select(User.account_level, Profile.role, User.token_version)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.id == uid)
        )
    ).one()
    return row.account_level, row.role, row.token_version


async def test_internal_seams_fail_closed_without_token(
    client: Client, monkeypatch: pytest.MonkeyPatch, db: DB
) -> None:
    """内部写缝未配 token 时 fail-closed(401)——不成为公网面。"""
    monkeypatch.setattr(settings, "auth_http_token", "")
    for path, payload in (
        ("/api/v1/auth/internal/grant", {"kind": "incubation", "user_id": 1}),
        (
            "/api/v1/auth/internal/verify-password",
            {"username": "bob", "password": "x"},
        ),
    ):
        r = await client.post(path, json=payload)
        assert r.status_code == 401
        r2 = await client.post(path, json=payload, headers=_auth_headers())
        assert r2.status_code == 401


async def test_grant_exam_unlock_upgrades_and_bumps_token(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """normal/member 用户经 exam_unlock 升到 admin，token_version+1。"""
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    uid = await _mk_user(db, "alice", account_level="normal", role="member")
    await db.commit()

    r = await client.post(
        "/api/v1/auth/internal/grant",
        json={
            "kind": "exam_unlock",
            "user_id": uid,
            "unlock_level": "admin",
            "unlock_role": "author",
        },
        headers=_auth_headers(),
    )
    # 端点经 override get_session 共享 db；写入需随外部 commit 落库
    await db.commit()
    assert r.status_code == 200
    assert r.json() == {"changed": 1}

    lvl, role, tv = await _level(db, uid)
    assert (lvl, role) == ("admin", "author")
    assert tv == 1


async def test_grant_exam_unlock_is_monotonic_no_downgrade(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单向：已是更低目标不降级（升 admin 后再次请求 normal→no-op，token 不二次 bump）。"""
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    uid = await _mk_user(db, "carol", account_level="normal", role="author")
    await db.commit()

    # 已 author/member-normal，试降级 unlock_level normal / unlock_role member → 无实际动作
    r = await client.post(
        "/api/v1/auth/internal/grant",
        json={
            "kind": "exam_unlock",
            "user_id": uid,
            "unlock_level": "normal",
            "unlock_role": "member",
        },
        headers=_auth_headers(),
    )
    await db.commit()
    lvl, role, tv = await _level(db, uid)
    assert (lvl, role) == ("normal", "author")
    assert r.json() == {"changed": 0}
    assert tv == 0


async def test_grant_incubation_sets_admin_and_member_role(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纳入成员：level→admin；member role→incubated_member；token bump。"""
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    uid = await _mk_user(db, "dave", account_level="normal", role="member")
    await db.commit()

    r = await client.post(
        "/api/v1/auth/internal/grant",
        json={"kind": "incubation", "user_id": uid},
        headers=_auth_headers(),
    )
    await db.commit()
    lvl, role, tv = await _level(db, uid)
    assert r.json() == {"changed": 1}
    assert (lvl, role) == ("admin", "incubated_member")
    assert tv == 1


async def test_verify_password_ok_and_mismatch(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify-password：正确凭据 true，错误/不存在 false，只读不改行。"""
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    uid = await _mk_user(db, "erin", account_level="normal", role="member")
    await db.commit()

    ok_path = "/api/v1/auth/internal/verify-password"
    good = await client.post(
        ok_path,
        json={"username": "erin", "password": "secretwxyz123"},
        headers=_auth_headers(),
    )
    bad = await client.post(
        ok_path,
        json={"username": "erin", "password": "wrongpass"},
        headers=_auth_headers(),
    )
    missing = await client.post(
        ok_path,
        json={"username": "nobody", "password": "x"},
        headers=_auth_headers(),
    )
    assert good.json() == {"ok": True}
    assert bad.json() == {"ok": False}
    assert missing.json() == {"ok": False}
    lvl, role, tv = await _level(db, uid)
    assert (lvl, role, tv) == ("normal", "member", 0)


# —— 以下为 service 层幂等回归（不经 HTTP），锁定 auth 官方原语语义 ——


async def test_authz_primitives_idempotent(db: DB) -> None:
    """授权原语第二次调用（无更高目标）为 no-op，token_version 不再 bump。"""
    uid = await _mk_user(db, "frank", account_level="admin", role="author")
    await db.commit()
    ch1 = await grant_exam_unlock(
        db, uid, unlock_level="admin", unlock_role="author"
    )
    assert ch1 == 0  # 已是目标 → 无动作
    lvl, role, tv = await _level(db, uid)
    assert (lvl, role, tv) == ("admin", "author", 0)
