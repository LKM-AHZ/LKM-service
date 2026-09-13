"""L1 本地进程内缓存：有界 TTL + LRU 逐出 + 删/清/reset。"""

import asyncio
from typing import Any

import app.core.local_cache as local_cache


async def test_set_get_roundtrip() -> None:
    local_cache.l1_set("k", {"a": 1}, ttl=60)
    assert local_cache.l1_get("k") == {"a": 1}


async def test_miss_returns_none() -> None:
    assert local_cache.l1_get("absent") is None


async def test_ttl_expiry() -> None:
    local_cache.l1_set("k", "v", ttl=0.01)
    assert local_cache.l1_get("k") == "v"
    await asyncio.sleep(0.02)
    assert local_cache.l1_get("k") is None


async def test_non_positive_ttl_not_cached() -> None:
    local_cache.l1_set("k", "v", ttl=0)
    assert local_cache.l1_get("k") is None


async def test_delete_and_clear() -> None:
    local_cache.l1_set("a", 1, ttl=60)
    local_cache.l1_set("b", 2, ttl=60)
    local_cache.l1_delete("a")
    assert local_cache.l1_get("a") is None
    local_cache.l1_clear()
    assert local_cache.l1_get("b") is None
    assert local_cache.l1_size() == 0


async def test_lru_eviction_on_maxsize() -> None:
    local_cache.reset(maxsize=2)
    local_cache.l1_set("a", 1, ttl=60)
    local_cache.l1_set("b", 2, ttl=60)
    local_cache.l1_get("a")  # 刷新 a 的 LRU 位置
    local_cache.l1_set("c", 3, ttl=60)
    assert local_cache.l1_size() == 2
    assert local_cache.l1_get("b") is None  # 最久未用被逐出
    assert local_cache.l1_get("a") == 1
    assert local_cache.l1_get("c") == 3


def test_reset_clears(monkeypatch: Any) -> None:
    local_cache.l1_set("k", "v", ttl=60)
    local_cache.reset()
    assert local_cache.l1_size() == 0
