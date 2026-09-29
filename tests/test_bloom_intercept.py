"""蓝图 §5.6：`user:snap` 读路径的**白名单布隆拦截点**。

位图 = 全部合法 user id；``definitely_absent`` 为真 ⇒ 该 id 从未存在 ⇒ 直接短路为「不存在」，
不打 AUTH/DB。这条路径是客户端可控的：``POST /api/v1/users/{user_id}/follow`` 的路径参数由
调用方给，每个新 uuid 都是一次冷 miss，负值缓存（按 id 存）拦不住。

**门禁是安全方向**：只有预热完成（打了 ``seeded`` 标记）才允许据布隆拒绝；未预热 / Redis 不可用 /
命令异常 / 开关关 → 一律不拦。拿一份可能残缺的位图拒绝用户是最坏结果。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest

import app.core.bloom as bloom
import app.core.redis as redis_mod
import app.core.user_cache as uc
from app.core.config import settings
from auth.snapshot import get_user_snapshot, get_user_snapshot_batch

_NEVER_EXISTED = uuid.UUID("00000000-0000-7000-8000-0000000000ff")


class _BoomDB:
    """一旦被触达就炸的 DB 桩：证明拦截路径**没有**回退上游。"""

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("拦截生效时不应触达上游 DB")


@pytest.fixture(autouse=True)
async def _isolated(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """逐测复位 Redis 单例；关 L1/singleflight（本文件断言只针对拦截与 L2 语义）。"""
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


class TestGate:
    """门禁：未预热一律不拦——这是「宁可漏拦、绝不误拒」的落点。"""

    async def test_not_seeded_never_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.add(str(uuid.uuid4()))
        # 位图里没有 _NEVER_EXISTED、也没预热标记 → 不拦
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is False

    async def test_seeded_marks_unadded_key_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        assert await bloom.mark_seeded() is True
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is True

    async def test_seeded_added_key_is_not_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        await bloom.add(str(uid))
        await bloom.mark_seeded()
        assert await bloom.definitely_absent(str(uid)) is False

    async def test_unmark_seeded_disables_interception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is True
        await bloom.unmark_seeded()
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is False

    async def test_seeded_marker_carries_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """标记必须带 TTL：预热任务长期停摆时自动退回「不拦」，而不是永远拿旧位图拒绝。"""
        fake = _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        assert await fake.ttl(bloom._seeded_key()) > 0

    async def test_many_returns_only_absent_subset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        present = uuid.uuid4()
        absent_a = uuid.uuid4()
        absent_b = uuid.uuid4()
        await bloom.add(str(present))
        await bloom.mark_seeded()

        got = await bloom.definitely_absent_many(
            [str(present), str(absent_a), str(absent_b)]
        )
        assert got == {str(absent_a), str(absent_b)}

    async def test_many_empty_input_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        assert await bloom.definitely_absent_many([]) == set()


class TestGateFailOpen:
    async def test_redis_unavailable_never_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_redis() -> None:
            return None

        monkeypatch.setattr(bloom.redis_client, "get_redis", _no_redis)
        assert await bloom.mark_seeded() is False
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is False

    async def test_redis_error_never_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Broken:
            def pipeline(self, *_: Any, **__: Any) -> Any:
                raise ConnectionError("redis down")

        async def _broken_redis() -> Any:
            return _Broken()

        monkeypatch.setattr(bloom.redis_client, "get_redis", _broken_redis)
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is False

    async def test_disabled_flag_never_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_fake_redis(monkeypatch)
        monkeypatch.setattr(settings, "bloom_filter_enabled", False)
        assert await bloom.mark_seeded() is False
        assert await bloom.definitely_absent(str(_NEVER_EXISTED)) is False


class TestReadSnapIntercept:
    async def test_absent_id_short_circuits_to_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        # 负值语义：调用方据此「不回退上游」
        assert await uc.read_snap_state(_NEVER_EXISTED) == (True, None)

    async def test_without_seed_it_is_a_plain_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        assert await uc.read_snap_state(_NEVER_EXISTED) == (False, None)

    async def test_batch_puts_absent_ids_into_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        other = uuid.uuid4()
        negative, data = await uc.read_snaps_state([_NEVER_EXISTED, other])
        assert negative == {_NEVER_EXISTED, other}
        assert data == {}


class TestSnapshotIntercept:
    """端到端：拦截生效时**不触达上游**（DB 桩一被调用就炸）。"""

    async def test_single_read_returns_none_without_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        assert await get_user_snapshot(_BoomDB(), user_id=_NEVER_EXISTED) is None

    async def test_batch_read_filters_absent_without_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.mark_seeded()
        out = await get_user_snapshot_batch(_BoomDB(), user_ids=[_NEVER_EXISTED])
        assert out == {}
