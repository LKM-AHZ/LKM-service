"""Redis 接入层：懒初始化异步客户端，未配置/不可用时返回 None（fail-open 前提）。"""

import asyncio
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None
_client_pool: Any = None  # 底层池引用（测试替换为 fakeredis）
_LOCK = asyncio.Lock()
_PING_TIMEOUT = 0.2  # 秒


def _is_enabled() -> bool:
    """未配置 redis_url 即视为关闭。"""
    return bool(settings.redis_url)


def is_enabled() -> bool:
    """Redis 是否已配置（公开只读判断；供 L1 缓存等「Redis 关闭则整体关闭」的 gate 复用）。"""
    return _is_enabled()


async def get_redis() -> Redis | None:
    """返回可用的 Redis 客户端；未启用或连接/探测失败返回 None。

    失败时返回 None（fail-open），由限流器据此放行。每次调用从共享单例返回。
    """
    global _client, _client_pool
    if not _is_enabled():
        return None
    if _client is not None:
        return _client
    async with _LOCK:
        if _client is not None:
            return _client
        try:
            _client_pool = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                # 每次命令的 socket 超时：Redis 半挂（网络黑洞）时命令最多等
                # 0.5s 即抛错，由调用方 fail-open 兜底，避免无限挂起拖死事件循环。
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
            )
            # 探测：PING 在极短超时内通过才视为可用
            try:
                assert await asyncio.wait_for(_client_pool.ping(), _PING_TIMEOUT)
            except Exception:
                await _client_pool.aclose()
                _client_pool = None
                return None
            _client = _client_pool
        except Exception:
            # 初始化或连接阶段任何异常都降级为 None
            _client = None
            _client_pool = None
        return _client


async def close_redis() -> None:
    """关闭并清空单例（应用收尾调用）。幂等。"""
    global _client, _client_pool
    if _client_pool is not None:
        with suppress(Exception):
            await _client_pool.aclose()
    _client = None
    _client_pool = None
