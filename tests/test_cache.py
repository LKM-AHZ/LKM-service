"""读热点缓存：键规范、fail-open、版本失效、cached_read 回填。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import core.redis as redis_mod
from core import cache as cache_mod
from core.cache import (
    bump_collection_version,
    cache_get,
    cache_invalidate,
    cache_set,
    cached_read,
    collection_version,
    make_key,
)
from core.config import settings


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    # setup 和 teardown 都彻底复位：close 旧连接再置 None，杜绝上一测试残留的单例
    # 或未关闭 pool 在本测试被 get_redis() 复用（偶发 cache_get 返回 None 的根因之一）。
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


async def _enable_fake_redis(monkeypatch: Any) -> Any:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis()

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    return fake


def test_make_key() -> None:
    """键规范：env 命名空间 + None 归并空段，分段用 | 连接。"""
    # 默认 env=dev → 前缀含命名空间，隔离 dev/prod 共用 Redis
    assert make_key("columns:list", None, 1, None) == "lkm:dev:columns:list:|1|"
    assert make_key("articles:by_slug", "hello") == "lkm:dev:articles:by_slug:hello"


def test_make_key_honors_env(monkeypatch) -> None:
    """env 变化 → key 命名空间变化，producion 不污染 dev 缓存。"""
    from core.config import settings

    monkeypatch.setattr(settings, "env", "production")
    assert make_key("columns:list", 1) == "lkm:production:columns:list:1"


async def test_cache_fail_open_when_disabled(monkeypatch) -> None:
    """Redis 未配置 → cache_get 返回 None（fail-open），不抛错。"""
    monkeypatch.setattr(settings, "redis_url", "")
    assert await cache_get("k") is None
    await cache_invalidate("k")  # 不抛错即可
    assert await collection_version("c") == "v0"


async def test_cache_get_set_roundtrip(monkeypatch) -> None:
    """写入后能读回同一结构化 JSON 值。"""
    await _enable_fake_redis(monkeypatch)
    await cache_set("k1", {"a": 1, "b": ["x"]}, 60)
    assert await cache_get("k1") == {"a": 1, "b": ["x"]}


async def test_cache_invalidate_deletes_key(monkeypatch) -> None:
    """显式失效后该键不可读。"""
    await _enable_fake_redis(monkeypatch)
    await cache_set("k2", 42, 60)
    assert await cache_get("k2") == 42
    await cache_invalidate("k2")
    assert await cache_get("k2") is None


async def test_cache_invalidate_routes_mixed_prefixes(monkeypatch: Any) -> None:
    """批量失效中的键可能被灰度路由到不同 Redis 后端。"""
    import fakeredis.aioredis

    monkeypatch.setattr(settings, "redis_url_secondary", "redis://secondary:6379/0")
    monkeypatch.setattr(settings, "redis_secondary_prefixes", "following")
    clients = {
        name: fakeredis.aioredis.FakeRedis(decode_responses=True)
        for name in ("primary", "secondary")
    }

    async def get_redis(key: str) -> Any:
        return clients["secondary" if redis_mod.is_secondary(key) else "primary"]

    monkeypatch.setattr(cache_mod.redis_client, "get_redis", get_redis)
    first = make_key("following", "user")
    second = make_key("board:ids", "user")
    await clients["secondary"].set(first, "old")
    await clients["primary"].set(second, "old")
    await cache_invalidate(first, second)
    assert await clients["secondary"].get(first) is None
    assert await clients["primary"].get(second) is None


async def test_collection_version_bump_invalidates_old(monkeypatch) -> None:
    """写后 bump 版本号 → 旧列表键（含旧版本前缀）与新版本键不同。"""
    await _enable_fake_redis(monkeypatch)
    v0 = await collection_version("columns")
    await cache_set(make_key("columns:list", v0, 1), "old", 60)
    assert await collection_version("columns") == "v0"
    await bump_collection_version("columns")
    v1 = await collection_version("columns")
    assert v1 != v0
    # 旧键仍在但已不被新读取路径使用；新键缺失（直查库语义）
    assert await cache_get(make_key("columns:list", v1, 1)) is None


async def test_collection_version_decodes_bytes(monkeypatch: Any) -> None:
    class Client:
        async def get(self, _key: str) -> bytes:
            return b"42"

    async def get_redis(_key: str) -> Client:
        return Client()

    monkeypatch.setattr(cache_mod.redis_client, "get_redis", get_redis)
    assert await collection_version("columns") == "42"


async def test_cached_read_loads_on_miss_and_hits(monkeypatch) -> None:
    """未命中执行 loader 回填；后续命中不再执行 loader。"""
    await _enable_fake_redis(monkeypatch)
    loader_calls = 0

    async def _loader() -> dict[str, Any]:
        nonlocal loader_calls
        loader_calls += 1
        return {"value": loader_calls}

    first = await cached_read(make_key("a", 1), 60, _loader)
    assert first == {"value": 1}
    second = await cached_read(make_key("a", 1), 60, _loader)
    assert second == {"value": 1}
    assert loader_calls == 1


async def test_cached_read_single_flight_on_concurrent_miss(monkeypatch) -> None:
    """同一 key 的并发 miss：单飞合并，loader 只执行一次，结果共享。"""
    await _enable_fake_redis(monkeypatch)
    in_flight = 0
    peak = 0
    loader_calls = 0

    async def _slow_loader() -> dict[str, Any]:
        nonlocal in_flight, peak, loader_calls
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)  # 让并发请求都进入争锁状态
        in_flight -= 1
        loader_calls += 1
        return {"done": True}

    key = make_key("single", "flight")
    results = await asyncio.gather(
        *(cached_read(key, 60, _slow_loader) for _ in range(10))
    )
    assert all(r == {"done": True} for r in results)
    assert loader_calls == 1  # 并发 miss 只加载一次
    assert peak == 1  # 任意时刻最多一个 loader 在跑


async def test_invalidation_during_load_prevents_stale_refill(monkeypatch: Any) -> None:
    await _enable_fake_redis(monkeypatch)
    key = make_key("detail", "in-flight")
    loading = asyncio.Event()
    finish = asyncio.Event()

    async def loader() -> dict[str, str]:
        loading.set()
        await finish.wait()
        return {"value": "old"}

    task = asyncio.create_task(cached_read(key, 60, loader))
    await loading.wait()
    await cache_invalidate(key)
    finish.set()
    assert await task == {"value": "old"}
    assert await cache_get(key) is None


async def test_new_cache_value_survives_older_loader(monkeypatch: Any) -> None:
    await _enable_fake_redis(monkeypatch)
    key = make_key("detail", "newer")
    loading = asyncio.Event()
    finish = asyncio.Event()

    async def loader() -> dict[str, str]:
        loading.set()
        await finish.wait()
        return {"value": "old"}

    task = asyncio.create_task(cached_read(key, 60, loader))
    await loading.wait()
    await cache_set(key, {"value": "new"}, 60)
    finish.set()
    assert await task == {"value": "old"}
    assert await cache_get(key) == {"value": "new"}


async def test_cached_read_repairs_invalid_json(monkeypatch: Any) -> None:
    fake = await _enable_fake_redis(monkeypatch)
    key = make_key("detail", "invalid-json")
    await fake.set(key, "{broken")
    calls = 0

    async def loader() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    assert await cached_read(key, 60, loader) == {"value": 1}
    assert await cached_read(key, 60, loader) == {"value": 1}
    assert calls == 1


async def test_cached_read_caches_null_with_null_ttl(monkeypatch) -> None:
    """loader 返回 None 且传 null_ttl → 写入空值标记；窗口内不再调 loader。"""
    await _enable_fake_redis(monkeypatch)
    loader_calls = 0

    async def _null_loader():
        nonlocal loader_calls
        loader_calls += 1
        return None

    key = make_key("null", "cache")
    # 未传 null_ttl：loader 返回 None 不缓存 → 每次都会再次调用 loader
    assert await cached_read(key, 60, _null_loader) is None
    assert await cached_read(key, 60, _null_loader) is None
    assert loader_calls == 2

    # 传入 null_ttl：第一次 None 后写标记，第二次直接命中标记返回 None，不再调 loader
    loader_calls = 0
    assert await cached_read(key, 60, _null_loader, null_ttl=30) is None
    assert await cached_read(key, 60, _null_loader, null_ttl=30) is None
    assert loader_calls == 1
