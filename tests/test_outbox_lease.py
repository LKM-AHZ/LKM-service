"""M1.2 outbox relay Redis leader 租约原语验收（不依赖真实 broker/Redis）。

用 fakeredis 在内存模拟 Redis；每测试彻底复位 _core 单例（同 test_cache.py 范式），
避免跨测试残留连接/键污染。断言相对当前过程实际不依赖全局计数，故直接查 fakeredis
键值即准。

验收锚定蓝图 M1.2（peer/seam 层）：
- 两个副本并发 NX 抢占:仅一个拿到 token（另一拿 None → followe 不轮询）；
- 非持有者 renew 失败(值 != token → Lua 返回 0)；
- 持有者 TTL 到期（模拟 time 前换算成对 fakeredis 的 expiry 缩短）后，另一副本可接管。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

import core.redis as redis_mod
from core import outbox_relay
from core.config import settings


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

    # decode_responses=True 与生产 redis_client(get_redis) 一致：lease CAS 比较的是 str token。
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    return fake


async def _client() -> Any:
    c = await redis_mod.get_redis()
    assert c is not None
    return c


async def test_only_one_replica_holds_lease(monkeypatch) -> None:
    _enable_fake_redis(monkeypatch)
    redis = await _client()
    t1 = await outbox_relay._acquire_lease(redis, 60)
    assert t1 is not None  # 复本A 当选
    t2 = await outbox_relay._acquire_lease(redis, 60)
    assert t2 is None  # 复本B 竞争失败 → 记录不轮询


async def test_non_owner_cannot_renew(monkeypatch) -> None:
    _enable_fake_redis(monkeypatch)
    redis = await _client()
    owner = await outbox_relay._acquire_lease(redis, 60)
    assert owner is not None
    # 另一副本试图用它错误的 token 续约——不应成功（防止误续双活）。
    assert await outbox_relay._renew_lease(redis, "not-the-owner-token", 60) is False


async def test_lease_expires_then_takeover(monkeypatch) -> None:
    _enable_fake_redis(monkeypatch)
    redis = await _client()
    owner = await outbox_relay._acquire_lease(redis, 60)
    assert owner is not None
    # 模拟持有者失联:把键 TTL 压到 0,另一副本随即接管（失联接管）。
    await redis.expire(outbox_relay._lease_key(), 0)
    takeover = await outbox_relay._acquire_lease(redis, 60)
    assert takeover is not None
    # 旧持有者错写续约（已非 leader）不生效,不复活为双 leader。
    assert await outbox_relay._renew_lease(redis, owner, 60) is False


async def test_owner_renew_keeps_lease(monkeypatch) -> None:
    _enable_fake_redis(monkeypatch)
    redis = await _client()
    token = await outbox_relay._acquire_lease(redis, 60)
    assert token is not None
    assert await outbox_relay._renew_lease(redis, token, 60) is True  # 持方续约成功
    # 释放后键应被删,他人能抢占。
    await outbox_relay._release_lease(redis, token)
    assert await outbox_relay._acquire_lease(redis, 60) is not None
