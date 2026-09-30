"""Redis 后端兼容性回归（Redis 7 / Dragonfly 共用同一套断言）。

本仓绝大多数 Redis 测试跑在 fakeredis 上，覆盖不到真实服务端的行为——Lua 引擎、
WATCH 事务、SCAN 游标收敛、pub/sub 投递、GETDEL 等。把 L2 后端从 Redis 7 换成
Dragonfly（RESP 兼容）时真正的风险恰恰落在这些地方，故本文件直接对**真实后端**逐族
验证代码实际用到的命令。两个后端都应全绿；任一失败即该后端不可用。

跑法：默认 redis:7-alpine，换 Dragonfly 用 LKM_IT_REDIS_IMAGE。
    LKM_IT_USE_TESTCONTAINERS=1 uv run pytest -m integration
    LKM_IT_USE_TESTCONTAINERS=1 \\
        LKM_IT_REDIS_IMAGE=docker.dragonflydb.io/dragonflydb/dragonfly:v1.40.2 \\
        uv run pytest -m integration
或对已有实例：LKM_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration

未设 LKM_REDIS_URL 且未启用 Testcontainers 时整体 skip（与既有集成测试一致）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError, WatchError

from core import redis as redis_core

pytestmark = pytest.mark.integration

_PREFIX = "int-test:compat:"


def _key(name: str) -> str:
    return f"{_PREFIX}{name}"


# —— 注入 get_redis 指向真实连接（模块层用例用）——

_orig_get_redis = redis_core.get_redis


def _patch_get_redis(client: Redis) -> None:
    async def _get(*_a: object, **_k: object) -> Redis:
        return client

    redis_core.get_redis = _get  # ty: ignore[invalid-assignment]  # runtime monkeypatch


def _restore_get_redis() -> None:
    redis_core.get_redis = _orig_get_redis  # type: ignore[assignment]


async def _clear(client: Redis) -> None:
    """清本文件用的键（含 cache_lock 派生的 lkm:lock: 前缀），避免污染与互相干扰。"""
    for pat in (f"{_PREFIX}*", f"lkm:lock:{_PREFIX}*"):
        keys = [k async for k in client.scan_iter(match=pat)]
        if keys:
            await client.delete(*keys)


@pytest.fixture()
async def real_redis() -> AsyncIterator[Redis]:
    """连接真实后端；不可用则 skip。function 作用域：连接与测试同 loop 生命周期。"""
    url = os.environ.get("LKM_REDIS_URL", "")
    if not url:
        pytest.skip("LKM_REDIS_URL 为空，跳过 Redis 集成测试")
    client = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - 环境不可达时的快路径
        await client.aclose()
        pytest.skip(f"无法连接真实后端: {exc}")
    _patch_get_redis(client)
    await _clear(client)
    try:
        yield client
    finally:
        await _clear(client)
        _restore_get_redis()
        with suppress(Exception):
            await client.aclose()


async def _wait_message(pubsub: Any, *, timeout_s: float = 5.0) -> str | None:
    """从 pubsub 取一条 message（跳过订阅确认帧），带总超时。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        # subscribe 的帧是 "message"，psubscribe 的是 "pmessage"——两者都要收
        if msg is not None and msg.get("type") in ("message", "pmessage"):
            data = msg.get("data")
            return data.decode() if isinstance(data, bytes) else data
        await asyncio.sleep(0.02)
    return None


class TestRespPrimitives:
    """逐族验证代码实际用到的命令——任一失败即该后端不可用。"""

    async def should_roundtrip_set_get(self, real_redis: Redis) -> None:
        await real_redis.set(_key("k"), "v")
        assert await real_redis.get(_key("k")) == "v"

    async def should_support_set_nx_ex_lease(self, real_redis: Redis) -> None:
        # outbox 租约 / 文件上传锁 / 迁移锁都用这个原语
        k = _key("lease")
        assert await real_redis.set(k, "t1", nx=True, ex=30) is True
        assert await real_redis.set(k, "t2", nx=True, ex=30) is None  # 已存在 → 不覆盖
        assert await real_redis.get(k) == "t1"
        assert await real_redis.ttl(k) > 0

    async def should_support_getdel(self, real_redis: Redis) -> None:
        # counters drain / 文件清理依赖「取回即删」的原子性
        k = _key("gd")
        await real_redis.set(k, "1")
        assert await real_redis.getdel(k) == "1"
        assert await real_redis.get(k) is None

    async def should_support_incrby_and_mget(self, real_redis: Redis) -> None:
        k = _key("cnt")
        await real_redis.incrby(k, 5)
        await real_redis.incrby(k, 2)
        assert await real_redis.get(k) == "7"
        await real_redis.set(_key("m1"), "a")
        await real_redis.set(_key("m2"), "b")
        assert await real_redis.mget([_key("m1"), _key("m2"), _key("absent")]) == [
            "a",
            "b",
            None,
        ]

    async def should_scan_all_keys_to_completion(self, real_redis: Redis) -> None:
        # counters/drain、上传清理、metrics_relay 都靠 scan_iter 收敛游标
        for i in range(50):
            await real_redis.set(f"{_PREFIX}scan:{i}", "x")
        seen = set()
        async for k in real_redis.scan_iter(match=f"{_PREFIX}scan:*", count=7):
            seen.add(k)
        assert len(seen) == 50

    async def should_support_bitmap(self, real_redis: Redis) -> None:
        # 应用层布隆过滤器的唯一原语（SETBIT/GETBIT），非 RedisBloom 模块
        k = _key("bitmap")
        for pos in (0, 7, 63, 1000):
            await real_redis.setbit(k, pos, 1)
        for pos in (0, 7, 63, 1000):
            assert await real_redis.getbit(k, pos) == 1
        assert await real_redis.getbit(k, 1) == 0

    async def should_support_sorted_set_window(self, real_redis: Redis) -> None:
        # 滑动窗口限流器：ZADD/ZCARD/ZREMRANGEBYSCORE
        k = _key("zset")
        await real_redis.zadd(k, {"a": 1.0, "b": 2.0, "c": 3.0})
        assert await real_redis.zcard(k) == 3
        await real_redis.zremrangebyscore(k, 0, 2.0)
        assert await real_redis.zcard(k) == 1

    async def should_support_set_type(self, real_redis: Redis) -> None:
        # feed 大 V 集合：SADD/SMEMBERS
        k = _key("set")
        await real_redis.sadd(k, "u1", "u2", "u2")
        assert set(await real_redis.smembers(k)) == {"u1", "u2"}


class TestTransactions:
    """WATCH/MULTI/EXEC：cache_lock 释放、user_cache CAS、outbox 续约都依赖它。"""

    async def should_commit_watched_transaction(self, real_redis: Redis) -> None:
        k = _key("tx")
        async with real_redis.pipeline(transaction=True) as pipe:
            await pipe.watch(k)
            assert await pipe.get(k) is None
            pipe.multi()
            pipe.set(k, "done")
            await pipe.execute()
        assert await real_redis.get(k) == "done"

    async def should_abort_on_watch_conflict(self, real_redis: Redis) -> None:
        # 语义核心：被监视键在他方写入后，EXEC 必须抛 WatchError，否则 CAS 形同虚设
        k = _key("txw")
        await real_redis.set(k, "orig")
        async with real_redis.pipeline(transaction=True) as pipe:
            await pipe.watch(k)
            await real_redis.set(k, "changed-by-other")  # 模拟他方写入
            pipe.multi()
            pipe.set(k, "mine")
            with pytest.raises(WatchError):
                await pipe.execute()
        assert await real_redis.get(k) == "changed-by-other"


class TestLua:
    """Lua：限流脚本用服务端 TIME，脚本加载走 SCRIPT LOAD + EVALSHA + NOSCRIPT 重载。"""

    async def should_run_time_in_lua(self, real_redis: Redis) -> None:
        # 限流脚本以 Redis 服务端 TIME 为窗口起点（各实例本机时钟不可信）
        res = await real_redis.eval("local t = redis.call('TIME'); return tonumber(t[1])", 0)
        assert int(res) > 1_600_000_000

    async def should_evalsha_and_recover_from_noscript(self, real_redis: Redis) -> None:
        # redis_limiter 走 SCRIPT LOAD + EVALSHA，且必须能从 NOSCRIPT 重载
        script = "return redis.call('GET', KEYS[1])"
        sha = await real_redis.script_load(script)
        k = _key("lua")
        await real_redis.set(k, "hello")
        assert await real_redis.evalsha(sha, 1, k) == "hello"
        # 模拟脚本缓存失效（重启 / SCRIPT FLUSH / 换实例）：EVALSHA 必须抛错，调用方的
        # 重载分支才可能触发；若静默成功，限流会走 fail-open 悄悄失效。
        # 只断言「抛 ResponseError」而不匹配文本——消息体各后端/各 redis-py 版本不同
        # （Redis 7 实测为 "No matching script. Please use EVAL."，不含 NOSCRIPT 码）。
        # 「重载后能否自愈」由 test_redis_limiter_integration 的 flush 用例把关。
        await real_redis.script_flush()
        with pytest.raises(ResponseError):
            await real_redis.evalsha(sha, 1, k)

    async def should_run_compare_and_delete_lua(self, real_redis: Redis) -> None:
        # files/blog/migration 三处 compare-and-del 同一模式
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) else return 0 end"
        )
        k = _key("cad")
        await real_redis.set(k, "tok")
        assert await real_redis.eval(script, 1, k, "wrong") == 0
        assert await real_redis.get(k) == "tok"  # 不匹配不删
        assert await real_redis.eval(script, 1, k, "tok") == 1
        assert await real_redis.get(k) is None


class TestPubSub:
    """pub/sub：L1 失效广播（SUBSCRIBE）与 WS 按用户频道的 PSUBSCRIBE。"""

    async def should_deliver_published_message(self, real_redis: Redis) -> None:
        chan = _key("chan")
        pubsub = real_redis.pubsub()
        try:
            await pubsub.subscribe(chan)
            await real_redis.publish(chan, "payload")
            assert await _wait_message(pubsub) == "payload"
        finally:
            await pubsub.aclose()

    async def should_deliver_pattern_message(self, real_redis: Redis) -> None:
        pubsub = real_redis.pubsub()
        try:
            await pubsub.psubscribe(f"{_PREFIX}user:*")
            await real_redis.publish(f"{_PREFIX}user:42", "hi")
            assert await _wait_message(pubsub) == "hi"
        finally:
            await pubsub.aclose()


class TestModulePaths:
    """走真实模块代码路径（而非裸命令），验证端到端一致。"""

    async def should_roundtrip_l2_lock(self, real_redis: Redis) -> None:
        from core.cache_lock import l2_lock
        from core.config import settings

        if not settings.cache_lock_enabled:
            pytest.skip("cache_lock 未启用，跳过")
        k = _key("lock")
        lock_key = f"lkm:lock:{k}"
        async with l2_lock(k) as held:
            assert held is True
            assert await real_redis.get(lock_key) is not None
        # 释放走 WATCH + token 比对 + MULTI/DEL：必须删掉自己那把锁
        assert await real_redis.get(lock_key) is None
