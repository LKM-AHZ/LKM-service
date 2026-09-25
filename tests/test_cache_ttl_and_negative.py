"""蓝图 §5.6 的两项「必须」：TTL 随机扰动防雪崩、负值缓存防穿透。

- **TTL 扰动**：同批写入的 key 若 TTL 完全相同会同时过期 → 缓存集体失效回源打 DB/AUTH。
  写入时按 ±30% 摊开；L1 因 TTL 兼作「陈旧窗口上界」只向下扰动。
- **负值缓存**：上游确认不存在的 user_id 写短 TTL 负值，窗口内不再回退上游。
  必须与既有 CAS/epoch 守卫兼容：真实值能覆盖负值、负值不能盖真实值、失效后负写被拒。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest

import app.core.redis as redis_mod
import app.core.user_cache as uc
from app.core.cache import cache_set, jitter_ttl
from app.core.config import settings


@pytest.fixture(autouse=True)
async def _reset_redis_and_disable_l1(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """逐测复位 Redis 单例；并关掉 L1，让断言只针对 L2（负值不进 L1，此处也免干扰）。"""
    monkeypatch.setattr(settings, "user_snap_l1_enabled", False)
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


# ── A1：TTL 随机扰动 ───────────────────────────────────────────────────────


class TestJitterTtl:
    def should_keep_base_within_plus_minus_30pct(self) -> None:
        for _ in range(300):
            assert 210 <= jitter_ttl(300) <= 390

    def should_only_shrink_when_lower_only(self) -> None:
        """L1 的 TTL 是陈旧窗口上界，放大就破坏该保证 → 只允许向下扰动。"""
        for _ in range(300):
            assert 210 <= jitter_ttl(300, lower_only=True) <= 300

    def should_vary_across_calls(self) -> None:
        """扰动要真的随机：全同值等于没扰动（同批 key 仍会同时过期）。"""
        assert len({jitter_ttl(300) for _ in range(50)}) > 1

    def should_floor_at_one_second(self) -> None:
        """Redis 的 ex 不接受 0/负值。"""
        assert jitter_ttl(0) == 1
        assert jitter_ttl(-5) == 1
        assert jitter_ttl(0.1) >= 1


async def test_cache_set_applies_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache_set 是所有 cached_read 类缓存 TTL 的公共收口点，扰动落在这里。"""
    fake = _enable_fake_redis(monkeypatch)
    await cache_set("k1", {"a": 1}, 300)
    ttl = await fake.ttl("k1")
    assert 210 <= ttl <= 390


async def test_user_snap_l2_write_applies_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """user:snap 走自己的 CAS 写路径（不经 cache_set），须单独接入扰动。"""
    _enable_fake_redis(monkeypatch)
    uid = uuid.uuid4()
    assert await uc.write_if_newer(uid, {"user_id": str(uid)}, 123, 0) is True
    # 用 redis 直接读 TTL；写入是 jitter 后的 TTL_ITEM_S(=300) ±30%
    redis = await redis_mod.get_redis()
    assert redis is not None
    ttl = await redis.ttl(uc.get_user_cache_key(uid))
    assert 210 <= ttl <= 390


# ── A2：负值缓存防穿透 ─────────────────────────────────────────────────────


class TestNegativeCache:
    async def should_report_negative_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        assert await uc.write_negative(uid, 0) is True

        negative, data = await uc.read_snap_state(uid)
        assert negative is True
        assert data is None
        # 旧包装契约不变：负值对 read_snap 仍是 None（未命中语义）
        assert await uc.read_snap(uid) is None

    async def should_let_real_value_override_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """负值 sv=0，任何真实值（正 sv）都应能覆盖它——否则新用户会被窗口期挡住。"""
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        await uc.write_negative(uid, 0)
        data = {"user_id": str(uid), "username": "alice"}
        assert await uc.write_if_newer(uid, data, 999, 0) is True

        negative, got = await uc.read_snap_state(uid)
        assert negative is False
        assert got is not None
        assert got["username"] == "alice"
        # 读侧 _normalize_snap 会把 user_id 从 JSON 串还原成 UUID（既有契约）
        assert got["user_id"] == uid

    async def should_refuse_negative_over_real_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反向不允许：已有真实值时不写负值（版本条件只拒「更旧」，0 不拒正 sv 由这里守）。"""
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        await uc.write_if_newer(uid, {"user_id": str(uid)}, 999, 0)
        assert await uc.write_negative(uid, 0) is False

        negative, got = await uc.read_snap_state(uid)
        assert negative is False
        assert got is not None

    async def should_refuse_negative_captured_before_invalidation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """过期 epoch 的负写必须被拒，否则「已删用户」的负值会在失效后复活。"""
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        await uc.invalidate_user_snap(uid)  # epoch 0 → 1
        assert await uc.write_negative(uid, 0) is False

        negative, _ = await uc.read_snap_state(uid)
        assert negative is False

    async def should_invalidate_clear_existing_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """负值也在快照键上，失效的 DEL 一并清掉它。"""
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        await uc.write_negative(uid, 0)
        await uc.invalidate_user_snap(uid)
        negative, _ = await uc.read_snap_state(uid)
        assert negative is False

    async def should_fail_open_without_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "redis_url", "")
        uid = uuid.uuid4()
        assert await uc.write_negative(uid, 0) is False
        assert await uc.read_snap_state(uid) == (False, None)


async def test_snapshot_skips_upstream_when_negative_cached(
    auth_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """负值命中时 get_user_snapshot 不再回退上游——这正是防穿透的收益。"""
    from auth import snapshot as snap_mod

    _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "user_snap_singleflight_enabled", False)

    calls = 0
    real_retrieve = snap_mod._retrieve_fields

    async def _counting_retrieve(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await real_retrieve(*args, **kwargs)

    monkeypatch.setattr(snap_mod, "_retrieve_fields", _counting_retrieve)

    missing = uuid.uuid4()
    assert await snap_mod.get_user_snapshot(auth_db, user_id=missing) is None
    assert calls == 1  # 首次确实拉了一次上游（并写下负值）
    assert await snap_mod.get_user_snapshot(auth_db, user_id=missing) is None
    assert calls == 1  # 第二次命中负值：不再打上游
