"""M3.B S3：monolith 鉴权 deps 的 authz seam 双态验收。

seam 关闭（默认）→ deps 走本地 DB 直读（既有行为，回归锚由其余套件覆盖）。
seam 开启 → deps 把「锁定 / token_version / 改密撤销 / 角色档」判给 auth internal authz，
并 **fail-closed**（缝不可用/5xx → 拒绝，绝不保守放行）。本文件用 MockTransport 注入假
auth server 信封，隔离验证 deps 侧的 seam 映射（不连真实网络）。
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import auth.user_http as user_http
from app.core.config import settings
from app.core.err import BizError
from app.modules.admin import deps as admin_deps
from app.modules.admin.deps import create_admin_access_token
from auth import deps as auth_deps
from auth.errors import AuthErr
from auth.models import Profile, User
from auth.security import create_access_token, hashpwd
from tests.conftest import DB


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """deps seam 用例（seam-off 本地读/锁判定）在 auth 面落真实用户。"""
    return auth_db


@pytest.fixture(autouse=True)
async def _reset_globals():
    """每用例前后清 _client_factory 与 token，杜绝跨用例污染。"""
    user_http._client_factory = None  # type: ignore[attr-defined]
    settings.auth_http_url = ""
    settings.auth_http_token = ""
    yield
    user_http._client_factory = None  # type: ignore[attr-defined]
    settings.auth_http_url = ""
    settings.auth_http_token = ""


def _enable_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_http_url", "http://auth")
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")


def _default_user_token(user_id: uuid.UUID) -> str:
    return create_access_token(
        user_id=user_id,
        account_level="normal",
        role="member",
        token_version=0,
        mfa_verified=False,
    )


def _inject_client(
    handler: Any,
) -> None:
    """把 seam 出站 client 换成 MockTransport handler（离线、确定性）。"""

    def _factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    user_http._client_factory = _factory  # type: ignore[attr-defined]


async def _mk_active(
    db: AsyncSession, prefix: str, *, locked: bool = False
) -> uuid.UUID:
    user = User(
        username=f"{prefix}_u",
        email=f"{prefix}@example.com",
        account_level="normal",
        token_version=0,
        hashed_password=await hashpwd("pw123456"),
        is_locked=locked,
        locked_until=(
            _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=10) if locked else None
        ),
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user.id


async def test_seam_on_settles_auth_not_local_db(db: DB, monkeypatch) -> None:
    """seam 开启：以 auth server 的裁决为准（DB 状态不同也能被 seam 结果覆盖，证明走缝）。"""
    _enable_seam(monkeypatch)
    uid = await _mk_active(db, "okx", locked=True)  # 本地已锁
    hit: dict[str, bool] = {"seen": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        hit["seen"] = True
        return httpx.Response(
            200,
            json={
                "ok": True,
                "cause": None,
                "account_level": "admin",
                "role": "author",
            },
        )

    _inject_client(handler)
    cu = await auth_deps._resolve_current_user(_default_user_token(uid), db)
    assert hit["seen"]  # 确实经 HTTP seam 而非本地 DB
    assert cu.account_level == "admin"
    assert cu.role == "author"


async def test_seam_reject_is_honored(db: DB, monkeypatch) -> None:
    """auth server 裁决 locked → deps 抛 ACCOUNT_LOCKED（本地无锁也拒绝 → 真值在 auth）。"""
    _enable_seam(monkeypatch)
    uid = await _mk_active(db, "rj")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "cause": "locked", "account_level": None, "role": None},
        )

    _inject_client(handler)
    with pytest.raises(BizError) as ei:
        await auth_deps._resolve_current_user(_default_user_token(uid), db)
    assert ei.value.errcode == AuthErr.ACCOUNT_LOCKED


async def test_seam_fail_closed_on_5xx(db: DB, monkeypatch) -> None:
    """auth server 5xx/不可达 → deps fail-closed（拒绝，不因“连不上”放行）。"""
    _enable_seam(monkeypatch)
    uid = await _mk_active(db, "e5")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _inject_client(handler)
    with pytest.raises(BizError):
        await auth_deps._resolve_current_user(_default_user_token(uid), db)


async def test_seam_off_stays_local_lock(db: DB, monkeypatch) -> None:
    """seam 关闭（默认）→ deps 走本地 DB 直读：本地锁定照拒（行为不因改造变）。"""
    uid = await _mk_active(db, "so", locked=True)
    with pytest.raises(BizError) as ei:
        await auth_deps._resolve_current_user(_default_user_token(uid), db)
    assert ei.value.errcode == AuthErr.ACCOUNT_LOCKED


# —— 后台 admin seam（get_current_admin require_admin=True） ——


async def _mk_admin(db: AsyncSession) -> tuple[uuid.UUID, str]:
    user = User(
        username="adminseam",
        email="adminseam@example.com",
        account_level="admin",
        token_version=0,
        hashed_password=await hashpwd("pw123456"),
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user.id, create_admin_access_token(user)


async def test_admin_seam_ok(db: DB, monkeypatch) -> None:
    """后台 seam：裁决 ok(admin) → 放行并返回权威 role/account_level。"""

    _enable_seam(monkeypatch)
    _uid, token = await _mk_admin(db)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "cause": None, "account_level": "admin", "role": "superadmin"},
        )

    _inject_client(handler)
    cu = await admin_deps.get_current_admin(
        request=_req_with_cookie(token), db=db, token=token
    )
    assert cu.account_level == "admin"
    assert cu.role == "superadmin"


async def test_admin_seam_fail_closed(db: DB, monkeypatch) -> None:
    """后台 seam：裁决 not_admin → 统一 FORBIDDEN。"""
    from app.core.err import CommonErr

    _enable_seam(monkeypatch)
    _uid, token = await _mk_admin(db)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "cause": "not_admin", "account_level": None, "role": None}
        )

    _inject_client(handler)
    with pytest.raises(BizError) as ei:
        await admin_deps.get_current_admin(_req_with_cookie(token), db=db, token=token)
    assert ei.value.errcode == CommonErr.FORBIDDEN


def _req_with_cookie(token: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(cookies={admin_deps.COOKIE_NAME: token})
