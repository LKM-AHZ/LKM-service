"""蓝图 §5.6：白名单位图的全量预热（`auth.bloom_seed`）与「新用户不被误拒」的硬回归。

预热的唯一职责是保证位图**完整**——每个合法 user id 都在里面。健全性靠三处：预热（存量）、
建号即 add（增量）、每日重跑（自愈）。门禁（``seeded`` 标记）只在该完整性成立时才打开。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.bloom as bloom
import app.core.redis as redis_mod
from app.core.config import settings
from auth import bloom_seed
from auth.service_auth import create_user_with_profile
from auth.snapshot import get_user_snapshot


class _SharedSession:
    """借用测试夹具会话跑预热，但屏蔽 close()（预热契约是「自开会话并 close」）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
async def _isolated(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(settings, "user_snap_l1_enabled", False)
    monkeypatch.setattr(settings, "user_snap_singleflight_enabled", False)
    monkeypatch.setattr(settings, "bloom_filter_enabled", True)
    monkeypatch.setattr(settings, "bloom_filter_error_rate", 1e-9)
    monkeypatch.setattr(settings, "auth_http_url", "")
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


def _enable_fake_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    return fake


def _point_seed_at(monkeypatch: pytest.MonkeyPatch, auth_db: AsyncSession) -> None:
    async def _factory() -> _SharedSession:
        return _SharedSession(auth_db)

    monkeypatch.setattr(bloom_seed, "_session_factory", _factory)


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []


class _EmptySession:
    """users 表为空（尚未建任何用户）的 auth 会话桩。"""

    async def execute(self, *_: Any, **__: Any) -> _EmptyResult:
        return _EmptyResult()

    async def close(self) -> None:
        return None


class TestBackfill:
    async def should_seed_all_ids_and_open_gate(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        _point_seed_at(monkeypatch, auth_db)
        user = await create_user_with_profile(
            auth_db,
            username=f"seed_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
        )

        total = await bloom_seed.backfill_user_ids()

        assert total >= 1
        assert await bloom.might_contain(str(user.id)) is True
        # 预热完成 → 门禁打开：从未存在的 id 现在可被判「一定不在」
        assert await bloom.definitely_absent(str(uuid.uuid4())) is True

    async def should_be_idempotent(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        _point_seed_at(monkeypatch, auth_db)
        await create_user_with_profile(
            auth_db, username=f"idem_{uuid.uuid4().hex[:8]}", hashed_password="x"
        )

        first = await bloom_seed.backfill_user_ids()
        second = await bloom_seed.backfill_user_ids()

        assert first >= 1 and second == first
        assert await bloom.definitely_absent(str(uuid.uuid4())) is True

    async def should_not_mark_on_empty_users(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空库不打标记——空白名单会把所有人拦掉，宁可整段不生效。"""

        async def _factory() -> _EmptySession:
            return _EmptySession()

        _enable_fake_redis(monkeypatch)
        monkeypatch.setattr(bloom_seed, "_session_factory", _factory)

        assert await bloom_seed.backfill_user_ids() == 0
        assert await bloom.definitely_absent(str(uuid.uuid4())) is False

    async def should_not_mark_on_partial_write(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任一页写入不完整即不打标记（残缺血名单会误拒合法用户）。"""
        _enable_fake_redis(monkeypatch)
        _point_seed_at(monkeypatch, auth_db)
        await create_user_with_profile(
            auth_db, username=f"part_{uuid.uuid4().hex[:8]}", hashed_password="x"
        )

        async def _partial(keys: Any) -> int:
            return 0

        monkeypatch.setattr(bloom, "add_many", _partial)

        assert await bloom_seed.backfill_user_ids() == 0
        assert await bloom.definitely_absent(str(uuid.uuid4())) is False


class TestNewUserIsNeverRejected:
    async def should_stay_readable_after_creation(
        self, auth_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**关键回归**：门禁已开、位图已预热，但一个**刚建号**的用户必须当场可读。

        这是「建号即 add」的存在理由：若漏了这一步，紧随其后的快照读（如 follow 新用户）
        会被布隆误判为「从未存在」——正是最坏结果。
        """
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()  # 位图已预热，但尚未包含将要新建的 id

        user = await create_user_with_profile(
            auth_db,
            username=f"fresh_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
        )

        assert await bloom.definitely_absent(str(user.id)) is False
        snap = await get_user_snapshot(auth_db, user_id=user.id)
        assert snap is not None
        assert snap.user_id == user.id
