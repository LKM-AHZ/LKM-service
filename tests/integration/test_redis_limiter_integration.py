"""RedisRateLimiter 集成测试（真实 Redis，验证 Lua 滑动窗口原子逻辑）。

仅当显式 `uv run pytest -m integration` 时运行；连接不到真实 Redis
（未设 LKM_REDIS_URL 或不可达）时整体 skip。日常 `uv run pytest` 不收集。
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
from redis.asyncio import Redis

from app.core import redis as redis_core
from app.core.err import BizError
from app.core.redis_limiter import RedisRateLimiter
from auth.errors import AuthErr

pytestmark = pytest.mark.integration


def _redis_url() -> str:
    return os.environ.get("LKM_REDIS_URL", "")


@pytest.fixture()
async def real_redis() -> AsyncIterator[Redis]:
    """连接真实 Redis；不可用则 skip。使用独立 key 前缀避免污染生产数据。

    function 作用域：让连接与测试同 loop 生命周期，避免 module teardown 时
    loop 已关闭导致的传输层关闭报错（pytest-asyncio 已知行为）。
    """
    url = _redis_url()
    if not url:
        pytest.skip("LKM_REDIS_URL 为空，跳过 Redis 集成测试")
    client = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - 环境不可达时的快路径
        await client.aclose()
        pytest.skip(f"无法连接真实 Redis: {exc}")
    _patch_get_redis(client)
    await _clear_keys(client)
    yield client
    _restore_get_redis()
    with suppress(Exception):
        await client.aclose()


# —— 注入 get_redis 指向真实连接 ——

_orig_get_redis = redis_core.get_redis


def _patch_get_redis(client: Redis) -> None:
    async def _get() -> Redis:
        return client

    redis_core.get_redis = _get  # ty: ignore[invalid-assignment]  # runtime monkeypatch


def _restore_get_redis() -> None:
    redis_core.get_redis = _orig_get_redis  # type: ignore[assignment]


_KEY_PREFIX = "int-test:limiter:"


def _key(name: str) -> str:
    return f"{_KEY_PREFIX}{name}"


async def _clear_keys(client: Redis) -> None:
    keys = [key async for key in client.scan_iter(match=f"{_KEY_PREFIX}*")]
    if keys:
        await client.delete(*keys)


class TestRedisRateLimiterIntegration:
    async def should_block_after_max_count(self, real_redis: Redis) -> None:
        k = _key("block")
        limiter = RedisRateLimiter()
        for _ in range(5):
            assert await limiter.check(k, max_count=5, window_seconds=10) is True
        assert await limiter.check(k, max_count=5, window_seconds=10) is False

    async def should_allow_after_reset(self, real_redis: Redis) -> None:
        k = _key("reset")
        limiter = RedisRateLimiter()
        for _ in range(5):
            await limiter.check(k, max_count=5, window_seconds=10)
        assert await limiter.check(k, max_count=5, window_seconds=10) is False
        await limiter.reset(k)
        assert await limiter.check(k, max_count=5, window_seconds=10) is True

    async def should_isolate_keys(self, real_redis: Redis) -> None:
        limiter = RedisRateLimiter()
        for _ in range(5):
            await limiter.check(_key("a"), max_count=5, window_seconds=10)
        assert await limiter.check(_key("a"), max_count=5, window_seconds=10) is False
        assert await limiter.check(_key("b"), max_count=5, window_seconds=10) is True

    async def should_release_after_window_expiry(self, real_redis: Redis) -> None:
        k = _key("expiry")
        limiter = RedisRateLimiter()
        for _ in range(5):
            await limiter.check(k, max_count=5, window_seconds=0.1)
        assert await limiter.check(k, max_count=5, window_seconds=0.1) is False
        await asyncio.sleep(0.15)
        assert await limiter.check(k, max_count=5, window_seconds=0.1) is True

    async def should_raise_biz_error_when_code_rate_limited(
        self, real_redis: Redis
    ) -> None:
        """check_code_rate_limit 超限时应抛 VERIFICATION_CODE_RATE_LIMIT。"""
        from auth.service_verify import check_code_rate_limit as ccrl

        for _ in range(5):
            await ccrl(_key("code-limited"), max_count=5, window=3600)
        with pytest.raises(BizError) as exc:
            await ccrl(_key("code-limited"), max_count=5, window=3600)
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_RATE_LIMIT
