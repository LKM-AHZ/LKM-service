"""蓝图 §5.6 布隆过滤器：Redis bitmap 跨进程、无假阴性、Redis 不可用时 fail-open 不拦。

布隆是「防穿透」里与空值缓存互补的一面：空值缓存挡「合法但查无」，布隆挡「非法/不可能
存在」的 key 形态。硬约束是**绝不误拦**——Redis 故障时必须 fail-open 返回 True（不拦），
宁可放过非法 key 交给下游兜底。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest

import app.core.bloom as bloom
import app.core.redis as redis_mod
import app.core.user_cache as uc
from app.core.config import settings


@pytest.fixture(autouse=True)
async def _reset_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """逐测复位 Redis 单例；关掉 L1（本文件断言只针对 L2/位数组）。

    误判率压到 1e-9：让「未加入的 key 一定为 False」成为确定性断言，而非撞 1% 假阳性。
    """
    monkeypatch.setattr(settings, "user_snap_l1_enabled", False)
    monkeypatch.setattr(settings, "bloom_filter_enabled", True)
    monkeypatch.setattr(settings, "bloom_filter_error_rate", 1e-9)
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


class TestParams:
    def test_defaults_yield_positive_m_and_k(self) -> None:
        m, k = bloom._params(100_000, 0.01)
        assert m > 0
        # p=0.01 → k≈7 是布隆最优轮数的标准结果
        assert k == 7

    def test_smaller_error_rate_needs_more_bits(self) -> None:
        m_loose, _ = bloom._params(100_000, 0.05)
        m_tight, _ = bloom._params(100_000, 0.001)
        assert m_tight > m_loose

    def test_invalid_inputs_are_clamped(self) -> None:
        # capacity<=0 / error_rate 越界不得算出 0/负位下标
        m, k = bloom._params(0, 0.0)
        assert m >= 1 and k >= 1
        m2, k2 = bloom._params(-5, 5.0)
        assert m2 >= 1 and k2 >= 1


class TestBitmapBloom:
    async def test_added_key_is_reported_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _enable_fake_redis(monkeypatch)
        assert await bloom.add("00000000-0000-7000-8000-000000000001") is True
        assert (
            await bloom.might_contain("00000000-0000-7000-8000-000000000001")
        ) is True
        # 位真的落在 Redis bitmap 上（跨进程可见的前提）
        assert await fake.bitcount(bloom._bitmap_key()) > 0

    async def test_never_added_key_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        await bloom.add("a")
        assert await bloom.might_contain("b") is False

    async def test_cross_visibility_via_shared_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两个「进程」共用同一 Redis bitmap → 一方 add、另一方即见（跨进程可见）。"""
        _enable_fake_redis(monkeypatch)
        await bloom.add("shared-id")
        # 直接绕过缓冲，读同一 Redis 上的位（等价另一副本的查询）
        assert await bloom.might_contain("shared-id") is True


class TestFailOpen:
    async def test_redis_unavailable_add_false_and_no_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_redis() -> None:
            return None

        monkeypatch.setattr(bloom.redis_client, "get_redis", _no_redis)
        assert await bloom.add("x") is False
        # 关键：拿不到 Redis 时绝不拦（True = 放过，交下游兜底）
        assert await bloom.might_contain("x") is True

    async def test_redis_error_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Broken:
            def pipeline(self, *_: Any, **__: Any) -> Any:
                raise ConnectionError("redis down")

        async def _broken_redis() -> Any:
            return _Broken()

        monkeypatch.setattr(bloom.redis_client, "get_redis", _broken_redis)
        assert await bloom.add("x") is False
        assert await bloom.might_contain("x") is True

    async def test_disabled_flag_never_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "bloom_filter_enabled", False)
        assert await bloom.add("x") is False
        assert await bloom.might_contain("x") is True


class TestUserCacheIntegration:
    async def test_write_negative_records_id_in_bloom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """负值缓存落定时把「上游确认不存在」的 id 记入布隆（§5.6 接入点）。"""
        _enable_fake_redis(monkeypatch)
        import uuid

        uid = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
        other = uuid.UUID("00000000-0000-7000-8000-0000000000bb")

        assert await uc.write_negative(uid, 0) is True
        assert await bloom.might_contain(str(uid)) is True
        assert await bloom.might_contain(str(other)) is False
