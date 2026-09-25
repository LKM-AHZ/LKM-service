"""跨进程缓存锁（B4）：互斥语义、释放的 token 守卫、fail-open、与 ``cached_read`` 的协作。

锁本身就是共享 Redis 上的 ``SET NX``，故**同一 Redis 上的两次 ``l2_lock`` 嵌套即等价于两个
进程**，无需真起多实例。**刻意不用 Lua 脚本**做释放（见 ``cache_lock`` 模块 docstring：
fakeredis 无脚本引擎，eval 失败会被 suppress 成「锁永远删不掉」的静默假绿）。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.core.redis as redis_mod
from app.core import cache as cache_mod
from app.core.cache import cached_read
from app.core.cache_lock import _release, l2_lock
from app.core.config import settings


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    """每个用例前后彻底复位 Redis 单例（照 tests/test_cache.py 范式）。"""
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


async def _enable_fake_redis(monkeypatch: Any) -> Any:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    return fake


async def test_second_caller_cannot_acquire(monkeypatch: Any) -> None:
    await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "cache_lock_wait_ms", 20)

    async with l2_lock("k1") as first:
        assert first is True
        async with l2_lock("k1") as second:
            assert second is False  # 等锁超时 → fail-open，交由调用方直读


async def test_lock_released_after_exit(monkeypatch: Any) -> None:
    await _enable_fake_redis(monkeypatch)

    async with l2_lock("k2") as held:
        assert held is True
    async with l2_lock("k2") as again:
        assert again is True, "退出上下文后锁必须已释放"


async def test_release_requires_matching_token(monkeypatch: Any) -> None:
    """用别人的 token 释放不掉锁——否则互斥会被破坏成空转。"""
    await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "cache_lock_wait_ms", 20)
    client = await redis_mod.get_redis()
    assert client is not None

    async with l2_lock("k3"):
        await _release(client, "lkm:lock:k3", "not-my-token")
        async with l2_lock("k3") as other:
            assert other is False, "异己 token 的释放不得生效"


async def test_fail_open_without_redis() -> None:
    """未配置 Redis（默认 settings.redis_url 空）→ 恒不持锁，行为与引入前一致。"""
    async with l2_lock("k4") as held:
        assert held is False


async def test_disabled_by_flag(monkeypatch: Any) -> None:
    await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "cache_lock_enabled", False)
    async with l2_lock("k5") as held:
        assert held is False


async def test_cached_read_only_holder_fills_l2(monkeypatch: Any) -> None:
    """持锁者才回填 L2；等锁超时的实例只直读、不写（避免覆盖持锁者的新值）。"""
    await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "cache_lock_wait_ms", 20)
    key = cache_mod.make_key("t", "only-holder")
    calls = {"n": 0}

    async def loader() -> dict[str, int]:
        calls["n"] += 1
        return {"v": calls["n"]}

    async with l2_lock(key) as outer:
        assert outer is True
        # 持锁期间另一"进程"读同一 key：取不到锁 → fail-open 直读，但不得回填 L2
        assert await cached_read(key, 60, loader) == {"v": 1}
        assert await cache_mod.cache_get(key) is None

    # 锁已释放：这次是持锁者，正常回填
    assert await cached_read(key, 60, loader) == {"v": 2}
    assert await cache_mod.cache_get(key) == {"v": 2}
    # 再读命中 L2，不再调 loader
    assert await cached_read(key, 60, loader) == {"v": 2}
    assert calls["n"] == 2
