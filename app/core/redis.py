"""Redis 接入层：懒初始化异步客户端，未配置/不可用时返回 None（fail-open 前提）。"""

import asyncio
import logging
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.core.secrets import reveal

logger = logging.getLogger(__name__)

_client: Redis | None = None
_client_pool: Any = None  # 底层池引用（测试替换为 fakeredis）
_LOCK = asyncio.Lock()
_PING_TIMEOUT = 0.2  # 秒


def _is_enabled() -> bool:
    """未配置 redis_url 即视为关闭。"""
    return bool(reveal(settings.redis_url))


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
                reveal(settings.redis_url),
                decode_responses=True,
                # 每次命令的 socket 超时：Redis 半挂（网络黑洞）时命令最多等
                # 0.5s 即抛错，由调用方 fail-open 兜底，避免无限挂起拖死事件循环。
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
            )
            # 探测：PING 在极短超时内通过才视为可用。不用 assert 表达——`python -O`
            # 会整句删除（含 wait_for），探测连同超时一起消失，不可用的 Redis 会被
            # 当成 "可用" 缓存进 _client，与 fail-open 契约相反。
            try:
                pong = await asyncio.wait_for(_client_pool.ping(), _PING_TIMEOUT)
                if not pong:
                    raise RuntimeError("redis ping 返回假值")
            except Exception as exc:
                # 必须留痕：否则 URL 配错/DNS/TLS 失败/宕机都表现为「无 Redis」，限流被静默
                # 关闭而无从排查。fail-open 返回值不变。
                logger.warning("redis ping 失败，降级为不可用: %s", exc)
                # aclose 自身失败也要继续走降级路径，否则异常冒到外层只置空引用、池未释放
                with suppress(Exception):
                    await _client_pool.aclose()
                _client_pool = None
                return None
            _client = _client_pool
        except Exception as exc:
            # 初始化或连接阶段任何异常都降级为 None
            logger.warning("redis 客户端初始化失败，降级为不可用: %s", exc)
            _client = None
            _client_pool = None
        return _client


async def close_redis() -> None:
    """关闭并清空单例（应用收尾调用）。幂等。

    与 ``get_redis`` 共用 ``_LOCK``：否则并发 ``get_redis`` 可能拿到一个正在 ``aclose``
    的池（连接已断），或在收尾清空后又新建一个绑在已关闭事件循环上的 client。
    """
    global _client, _client_pool
    async with _LOCK:
        if _client_pool is not None:
            with suppress(Exception):
                await _client_pool.aclose()
        _client = None
        _client_pool = None
