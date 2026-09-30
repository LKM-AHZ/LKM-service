"""RedisRateLimiter 单元测试：不含 Lua 脚本的行为（fail-open、reset 静默）。

滑动窗口正确性（超限/隔离/窗口过期）依赖 Lua 脚本，fakeredis 无 Lua 引擎，
由集成测试 tests/integration/test_redis_limiter_integration.py 用真实 Redis 覆盖。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from core import redis as redis_core
from core.redis_limiter import RedisRateLimiter


@pytest.fixture(autouse=True)
async def isolated_get_redis() -> AsyncIterator[None]:
    """每用例后恢复 get_redis 的原始注入，隔离测试。"""
    original = redis_core.get_redis
    yield
    redis_core.get_redis = original  # type: ignore[assignment]


class TestRedisRateLimiter:
    async def should_fail_open_when_redis_unavailable(self) -> None:
        """get_redis 返回 None（URL 未配置）时一律放行。"""

        async def _none(*_a: Any, **_k: Any) -> Any:
            return None

        redis_core.get_redis = _none  # ty: ignore[invalid-assignment]  # runtime monkeypatch 临时替换签名
        limiter = RedisRateLimiter()
        assert await limiter.check("x", max_count=1, window_seconds=10) is True

    async def should_fail_open_when_script_fails(self) -> None:
        """get_redis 返回的客户端在 Lua 脚本执行时抛异常 → 放行（fail-open）。"""

        class _BrokenGetRedis:
            async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("lua executor down")

        async def _broken(*_a: Any, **_k: Any) -> Any:
            return _BrokenGetRedis()

        redis_core.get_redis = _broken  # ty: ignore[invalid-assignment]  # runtime monkeypatch 临时替换签名
        limiter = RedisRateLimiter()
        assert await limiter.check("x", max_count=1, window_seconds=10) is True

    async def should_reset_be_noop_when_redis_unavailable(self) -> None:
        """get_redis 返回 None 时 reset 静默无操作、不抛异常。"""

        async def _none(*_a: Any, **_k: Any) -> Any:
            return None

        redis_core.get_redis = _none  # ty: ignore[invalid-assignment]  # runtime monkeypatch 临时替换签名
        limiter = RedisRateLimiter()
        await limiter.reset("x")  # 不应抛异常
