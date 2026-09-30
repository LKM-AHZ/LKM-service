"""T1：``token_version`` 并入撤销预检缓存（蓝图 §4.2）的验收。

- 语义：缓存存「当前最新版本」（可接受的最低版本）；token 携带版本**低于**缓存 → 陈旧（加拒）；
  相等/更高/未命中/无版本/Redis 不可用 → 均不陈旧（**绝不据此放行**）。
- 写入侧单一维护点：``UserRepository.bump_token_version`` 成功后写缓存。
- deps 鉴权路径：陈旧版本在 DB 之前按既有 TOKEN_EXPIRED 拒；版本相等**不**放行，仍回查 DB
  （以 USER_NOT_FOUND 证明 DB 被判据）。
fixture 沿用仓库 fakeredis 范式（reset 单例 + settings.redis_url + Redis.from_url→fake）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import auth.deps as auth_deps
import auth.token_revocation as tv
import core.redis as redis_mod
from auth.errors import AuthErr
from auth.models import User
from auth.repository import UserRepository
from auth.security import create_access_token, hashpwd
from core.config import settings
from core.err import BizError

UID = uuid.UUID("00000000-0000-7000-8000-00000000abc1")


@pytest.fixture(autouse=True)
async def _reset_redis() -> AsyncIterator[None]:
    """每用例前后复位 redis 单例，杜绝跨用例残留。"""
    await redis_mod.close_redis()
    redis_mod._client = None  # type: ignore[attr-defined]
    redis_mod._client_pool = None  # type: ignore[attr-defined]
    yield
    await redis_mod.close_redis()
    redis_mod._client = None  # type: ignore[attr-defined]
    redis_mod._client_pool = None  # type: ignore[attr-defined]


def _enable_fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: object, url: str, **kwargs: object) -> object:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))


def _token(uid: uuid.UUID, version: int) -> str:
    return create_access_token(
        user_id=uid,
        account_level="normal",
        role="member",
        token_version=version,
        mfa_verified=False,
    )


async def _mk_user(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        account_level="normal",
        token_version=0,
        hashed_password=await hashpwd("pw123456"),
    )
    db.add(user)
    await db.flush()
    return user


class TestCacheSemantics:
    async def test_only_lower_version_is_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        assert await tv.set_token_version(UID, 7) is True
        assert await tv.token_version_is_stale(UID, 3) is True  # 低于缓存 → 陈旧
        assert (
            await tv.token_version_is_stale(UID, 7) is False
        )  # 相等不算陈旧（不放行）
        assert await tv.token_version_is_stale(UID, 8) is False  # 更高不陈旧
        assert (
            await tv.token_version_is_stale(UID, None) is False
        )  # 无版本旧 token 跳过
        assert await tv.token_version_is_stale(uuid.uuid4(), 0) is False  # 未命中

    async def test_no_redis_is_silent_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "redis_url", "")
        assert await tv.set_token_version(UID, 5) is False
        assert await tv.token_version_is_stale(UID, 1) is False


class TestBumpWritesCache:
    async def test_bump_writes_new_version_to_cache(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        user = await _mk_user(auth_db, "tvbump")
        new_version = await UserRepository(auth_db).bump_token_version(user.id)
        assert new_version == 1
        assert await tv.token_version_is_stale(user.id, 0) is True
        assert await tv.token_version_is_stale(user.id, 1) is False

    async def test_bump_without_redis_does_not_raise(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "redis_url", "")
        user = await _mk_user(auth_db, "tvnoredis")
        assert await UserRepository(auth_db).bump_token_version(user.id) == 1


class TestDepsPrecheck:
    async def test_stale_rejected_before_db(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存版本 5 > token 版本 3，且该 uid **不在 DB** → 必须报 TOKEN_EXPIRED 而非
        USER_NOT_FOUND：证明拒绝发生在 DB 查询之前（预检短路）。"""
        _enable_fake_redis(monkeypatch)
        monkeypatch.setattr(settings, "auth_http_url", "")
        monkeypatch.setattr(settings, "auth_http_token", "")
        await tv.set_token_version(UID, 5)
        with pytest.raises(BizError) as ei:
            await auth_deps._resolve_current_user(_token(UID, 3), auth_db)
        assert ei.value.errcode == AuthErr.TOKEN_EXPIRED
        assert "Session invalidated" in ei.value.detail

    async def test_equal_version_does_not_allow(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """版本相等 → 预检不拒也**不放行**：仍回查 DB（uid 不存在 → USER_NOT_FOUND）。"""
        _enable_fake_redis(monkeypatch)
        monkeypatch.setattr(settings, "auth_http_url", "")
        monkeypatch.setattr(settings, "auth_http_token", "")
        await tv.set_token_version(UID, 5)
        with pytest.raises(BizError) as ei:
            await auth_deps._resolve_current_user(_token(UID, 5), auth_db)
        assert ei.value.errcode == AuthErr.USER_NOT_FOUND
