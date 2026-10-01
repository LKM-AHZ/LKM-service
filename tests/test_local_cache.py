"""L1 本地进程内缓存：有界 TTL + LRU 逐出 + 删/清/reset。"""

import asyncio
from typing import Any

import core.local_cache as local_cache


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


def test_multi_get_preserves_order_and_lru(monkeypatch: Any) -> None:
    local_cache.reset(maxsize=2)
    monkeypatch.setattr(local_cache, "_now", lambda: 100.0)
    local_cache.l1_set("a", "A", ttl=10)
    local_cache.l1_set("b", "B", ttl=10)
    assert local_cache.l1_multi_get(["a", "missing", "a"]) == ["A", None, "A"]
    local_cache.l1_set("c", "C", ttl=10)
    assert local_cache.l1_get("b") is None
    assert local_cache.l1_get("a") == "A"


def test_conditional_fill_rejects_invalidation_and_newer_value() -> None:
    local_cache.reset()
    revision = local_cache.l1_invalidation_revision()
    local_cache.l1_delete("key")  # 失效时键尚不存在，也必须阻止在途旧回填
    assert not local_cache.l1_set_if_unchanged("key", "old", 60, revision)
    assert local_cache.l1_get("key") is None

    revision = local_cache.l1_invalidation_revision()
    local_cache.l1_set("key", "new", 60)
    assert not local_cache.l1_set_if_unchanged("key", "old", 60, revision)
    assert local_cache.l1_get("key") == "new"


def test_conditional_fill_accepts_unchanged_key() -> None:
    local_cache.reset()
    revision = local_cache.l1_invalidation_revision()
    assert local_cache.l1_set_if_unchanged("key", "value", 60, revision)
    assert local_cache.l1_get("key") == "value"


def test_reset_clears(monkeypatch: Any) -> None:
    local_cache.l1_set("k", "v", ttl=60)
    local_cache.reset()
    assert local_cache.l1_size() == 0
