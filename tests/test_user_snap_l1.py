"""user:snap 双级缓存 L1：镜像语义、CAS 不污染、失效清本地 + 广播订阅。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.core.local_cache as local_cache
import app.core.redis as redis_mod
import app.core.user_cache as uc
import app.core.user_cache_events as uce
from app.core.config import settings

_SNAP = {
    "user_id": 7,
    "username": "bob",
    "display_name": "Bob",
    "avatar": None,
    "role": None,
    "account_level": "local",
    "banned": False,
    "nickname": None,
}


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


def _enable_fake_redis(monkeypatch: Any) -> Any:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    return fake


async def _wait_until(pred: Any, limit_s: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + limit_s
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


async def test_l2_hit_backfills_l1_then_l1_serves(monkeypatch: Any) -> None:
    fake = _enable_fake_redis(monkeypatch)
    key = uc.get_user_cache_key(7)
    await fake.set(key, json.dumps({"sv": 5, "data": _SNAP}), ex=300)

    assert await uc.read_snap(7) == _SNAP  # L2 命中并回填 L1

    await fake.delete(key)  # 绕开失效，直接删 L2
    assert await uc.read_snap(7) == _SNAP  # 仍由 L1 命中（证明 L1 生效）
    sv, data = await uc.read_snap_with_version(7)
    assert sv == 5 and data == _SNAP


async def test_write_if_newer_mirrors_l1(monkeypatch: Any) -> None:
    fake = _enable_fake_redis(monkeypatch)
    key = uc.get_user_cache_key(7)
    ok = await uc.write_if_newer(7, _SNAP, source_version=9, expected_epoch=0)
    assert ok is True
    await fake.delete(key)  # L2 去掉，读应来自 L1
    assert await uc.read_snap(7) == _SNAP


async def test_cas_reject_does_not_pollute_l1(monkeypatch: Any) -> None:
    fake = _enable_fake_redis(monkeypatch)
    key = uc.get_user_cache_key(7)
    newer = {"sv": 20, "data": {**_SNAP, "display_name": "Newer"}}
    await fake.set(key, json.dumps(newer), ex=300)
    assert local_cache.l1_get(key) is None  # 前置：L1 空

    # 陈旧 sv 回填被 L2 CAS 拒绝 → 不写也不删 L1
    ok = await uc.write_if_newer(7, _SNAP, source_version=1, expected_epoch=0)
    assert ok is False
    assert local_cache.l1_get(key) is None


async def test_invalidate_clears_local_l1_and_broadcasts(monkeypatch: Any) -> None:
    _enable_fake_redis(monkeypatch)
    key = uc.get_user_cache_key(7)
    assert await uc.write_if_newer(7, _SNAP, source_version=9, expected_epoch=0)
    assert local_cache.l1_get(key) is not None  # 已镜像进 L1

    # 另起一个「实例」的 L1 条目，验证订阅广播会删掉它（同进程内以另一个 key 模拟他实例副本）
    other_key = uc.get_user_cache_key(8)
    local_cache.l1_set(other_key, {"sv": 1, "data": _SNAP}, ttl=60)

    await uce.start()
    try:
        await uc.invalidate_user_snap(7)
        assert local_cache.l1_get(key) is None  # 本地 L1 立即删
        assert await uc.read_snap(7) is None  # L2 也已 DEL

        # 广播可达：订阅 task 收到后会删本地副本。用 uid=8 的 key 走同一广播通道验证通路。
        # 订阅注册有异步延迟，故重发直到生效（或超时失败），避免与订阅建立竞态 flake。
        cleared = False
        for _ in range(40):
            await uce.publish_invalidate(other_key)
            if await _wait_until(
                lambda: local_cache.l1_get(other_key) is None, limit_s=0.05
            ):
                cleared = True
                break
        assert cleared
    finally:
        await uce.stop()


async def test_redis_disabled_l1_not_served(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    key = uc.get_user_cache_key(7)
    local_cache.l1_set(key, {"sv": 1, "data": _SNAP}, ttl=60)
    assert await uc.read_snap(7) is None  # Redis 关闭 → 整个缓存（含 L1）关闭
