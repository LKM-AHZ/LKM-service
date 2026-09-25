"""access token 的 jti 撤销快速预检（蓝图 §4.2/§5.6）。

覆盖：载荷带 jti（每枚独立）、黑名单读写往返、**Redis 不可用时 fail-open**、无 jti 的旧
token 一律跳过（灰度零破坏）、校验路径命中黑名单即拒**且错误码与既有 token_version 路径
一致**、admin 登出后 cookie 立即失效（补「admin cookie 残留 ≤15min」的缺口）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
from app.core.config import settings
from app.core.err import AuthErr, BizError, CommonErr
from auth.admin_router import _require_admin_from_cookie
from auth.admin_session import (
    COOKIE_NAME,
    create_admin_access_token,
    decode_admin_access,
)
from auth.deps import _resolve_current_user
from auth.models import User
from auth.security import create_access_token, decode_access_token
from auth.token_revocation import (
    block_jti,
    block_payload_jti,
    is_jti_blocked,
)


@pytest.fixture(autouse=True)
async def _reset_redis_globals() -> AsyncIterator[None]:
    """Redis 客户端是模块级单例：逐测复位，避免上测的 fakeredis 泄漏到下一测。"""
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


def _enable_fake_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    return fake


def _access_token() -> str:
    return create_access_token(
        user_id=uuid.uuid4(), account_level="normal", role="member"
    )


# ── 载荷 ───────────────────────────────────────────────────────────────────


def test_access_token_carries_unique_jti() -> None:
    """每枚 access token 带独立 jti——共用标识会让单设备撤销误伤其他会话。"""
    ja = decode_access_token(_access_token())["jti"]
    jb = decode_access_token(_access_token())["jti"]
    assert isinstance(ja, str) and ja
    assert ja != jb


def test_admin_access_token_carries_jti() -> None:
    user = SimpleNamespace(id=uuid.uuid4(), account_level="admin", token_version=0)
    payload = decode_admin_access(create_admin_access_token(user))
    assert isinstance(payload.get("jti"), str) and payload["jti"]


# ── 黑名单读写 ─────────────────────────────────────────────────────────────


async def test_block_then_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_fake_redis(monkeypatch)
    assert await is_jti_blocked("j1") is False
    assert await block_jti("j1", 60) is True
    assert await is_jti_blocked("j1") is True
    assert await is_jti_blocked("j2") is False  # 只废被拉黑的那一枚


async def test_fail_open_when_redis_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 未配置：预检跳过（False），写入返回 False 让调用方回退 DB 权威手段。"""
    monkeypatch.setattr(settings, "redis_url", "")
    assert await is_jti_blocked("j1") is False
    assert await block_jti("j1", 60) is False


async def test_legacy_token_without_jti_skips_precheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 jti 的旧 token 一律跳过预检：灰度期内不能因取不到标识就全数拒绝。"""
    _enable_fake_redis(monkeypatch)
    assert await is_jti_blocked(None) is False
    assert await is_jti_blocked("") is False
    assert await block_payload_jti({"exp": 1}) is False


# ── 前台校验路径 ───────────────────────────────────────────────────────────


async def test_resolve_current_user_rejects_blocked_jti(
    auth_db: AsyncSession, auth_user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中黑名单即拒，且**错误码与 token_version 撤销路径一致**（客户端语义不因来源而变）。"""
    _enable_fake_redis(monkeypatch)
    user = await auth_user_factory(username="jti_blocked", role="member")
    await block_jti(decode_access_token(user.token)["jti"], 60)

    with pytest.raises(BizError) as ei:
        await _resolve_current_user(user.token, auth_db)
    assert ei.value.errcode == AuthErr.TOKEN_EXPIRED


async def test_resolve_current_user_ok_when_jti_not_blocked(
    auth_db: AsyncSession, auth_user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预检放行不改变正常路径：未被拉黑的 token 照常解析出用户。"""
    _enable_fake_redis(monkeypatch)
    user = await auth_user_factory(username="jti_ok", role="member")
    cur = await _resolve_current_user(user.token, auth_db)
    assert cur.id == user.id


# ── admin 会话（登出即时失效的缺口） ────────────────────────────────────────


async def test_admin_cookie_rejected_after_jti_blocked(
    auth_db: AsyncSession, auth_user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """admin 登出按 jti 拉黑后，该 access cookie **立即**失效（不必等 15min 自然过期）。"""
    _enable_fake_redis(monkeypatch)
    admin = await auth_user_factory(
        username="jti_admin", account_level="admin", role="admin"
    )
    row = (
        (await auth_db.execute(select(User).where(User.id == admin.id)))
        .scalars()
        .first()
    )
    assert row is not None
    token = create_admin_access_token(row)
    request = SimpleNamespace(cookies={COOKIE_NAME: token})

    # 未拉黑：可识别（先证明这条路径本身是通的，否则下面的拒绝毫无意义）
    assert (await _require_admin_from_cookie(request, auth_db)).id == admin.id

    await block_payload_jti(decode_admin_access(token))
    with pytest.raises(BizError) as ei:
        await _require_admin_from_cookie(request, auth_db)
    assert ei.value.errcode == CommonErr.FORBIDDEN


# ── 登出端到端 ─────────────────────────────────────────────────────────────


async def test_logout_blocks_own_access_token(
    auth_app_client: Any, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登出把本枚 access token 的 jti 写入黑名单，同一 token 随后立即被拒。

    token_version 也已 bump（全端失效），此处专门断言**这一枚**被拉黑：它是「单设备撤销」
    原语的消费点，且让拒答发生在 DB 往返之前。
    """
    from auth.schemas import UserLoginPassword, UserRegLocal
    from auth.service_auth import login_password, register_local

    _enable_fake_redis(monkeypatch)
    await register_local(
        auth_db, UserRegLocal(username="jti_logout", password="secret123456")
    )
    tokens = await login_password(
        auth_db, UserLoginPassword(account="jti_logout", password="secret123456")
    )
    access = tokens["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    # 先证明这条路径本身是通的，否则下面的拒绝没有信息量
    assert (
        await auth_app_client.get("/api/v1/auth/me", headers=headers)
    ).status_code == 200

    assert (
        await auth_app_client.post("/api/v1/auth/logout", headers=headers)
    ).status_code == 200

    assert await is_jti_blocked(decode_access_token(access)["jti"]) is True
    # 同一 token 立即被拒，不必等 15min 自然过期
    assert (
        await auth_app_client.get("/api/v1/auth/me", headers=headers)
    ).status_code == 401
