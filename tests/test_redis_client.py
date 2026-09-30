"""Redis 接入层测试：URL 为空时不启用、健康探测降级、fail-open 返回 None。"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

import core.redis as redis_mod
from core.config import settings


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    """每个用例前后彻底复位模块级单例（close 旧连接 + 置 None），杜绝跨测试残留。"""
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


async def test_default_disabled_when_url_empty(monkeypatch) -> None:
    """URL 为空串 → get_redis 返回 None，不尝试连接。"""
    monkeypatch.setattr(settings, "redis_url", "")
    assert await redis_mod.get_redis() is None


async def test_connect_failure_returns_none(monkeypatch) -> None:
    """URL 配置了但探测失败 → 返回 None（fail-open 前提）。"""
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:1")
    assert await redis_mod.get_redis() is None


async def test_healthy_returns_pingable_client(monkeypatch) -> None:
    """URL 配置且 ping 可用 → 返回可 ping 的客户端（fakeredis 注入）。"""
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis()

    def _fake_from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_fake_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:1")

    client = await redis_mod.get_redis()
    assert client is not None
    assert await client.ping() is True  # type: ignore[union-attr]
