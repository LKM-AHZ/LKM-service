"""
跨进程缓存互斥锁。
**失败模式全部 fail-open**（宁可多查一次 DB，不可让读阻塞或抛错）：
- 持锁者崩溃 → 锁的 TTL 自动过期，无需人工清理（无 TTL 的锁正是死锁的来源）；
- 等锁超时（``cache_lock_wait_ms``）→ 放弃等待、走无锁直读，**不制造死锁**；记``cache_lock_total{result=timeout}`` 以便观察。
- Redis 锁命令失败 → 立即降级，不再轮询到超时；记 ``result=error``。
- 释放用 ``WATCH`` + token 比对 + ``MULTI/DEL``
与 ``redis.set(nx=True)`` 的既有先例（``auth/user_dim_sync.py``、``db/migration_lock.py``）
同款原语，差别在本模块是**短 TTL + 可放弃等待**的读路径锁，不追求严格互斥。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from core import redis as redis_client
from core.config import settings
from core.metrics import cache_lock_total

logger = logging.getLogger("lkm.cache.lock")

_LOCK_PREFIX = "lkm:lock:"
_POLL_INTERVAL_S = 0.02


def _lock_key(key: str) -> str:
    """锁键由缓存键派生（缓存键已含 env 命名空间，故此处不再重复加）。"""
    return f"{_LOCK_PREFIX}{key}"


async def _acquire(client: Any, lock_key: str, token: str, ttl: int) -> bool | None:
    """True=持锁，False=被占用，None=Redis 命令失败。"""
    try:
        return bool(await client.set(lock_key, token, nx=True, ex=ttl))
    except Exception:
        logger.debug("cache lock acquire skip key=%s", lock_key)
        return None


async def _wait_for_lock(client: Any, lock_key: str, token: str, ttl: int) -> bool | None:
    """在 ``cache_lock_wait_ms`` 内轮询重试（等锁期间持锁者通常已完成回填）。"""
    deadline = time.monotonic() + max(0, settings.cache_lock_wait_ms) / 1000.0
    while (remaining := deadline - time.monotonic()) > 0:
        await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
        acquired = await _acquire(client, lock_key, token, ttl)
        if acquired is not False:
            return acquired
    return False


async def _release(client: Any, lock_key: str, token: str) -> None:
    """
    只删**自己持有的**锁：WATCH + token 比对 + MULTI/DEL。
    不做 token 比对直接 DEL 会误删「自己超时后他人重新获取的锁」，把互斥破坏成空转。
    """
    try:
        async with client.pipeline(transaction=True) as pipe:
            await pipe.watch(lock_key)
            current = await pipe.get(lock_key)
            if isinstance(current, bytes):
                current = current.decode()
            if current != token:
                await pipe.unwatch()
                return
            pipe.multi()
            pipe.delete(lock_key)
            await pipe.execute()
    except Exception:
        # 释放失败只降级为「锁靠 TTL 自解」，不影响调用方
        logger.debug("cache lock release skip key=%s", lock_key)


@asynccontextmanager
async def l2_lock(key: str) -> AsyncGenerator[bool]:
    """
    尝试获取跨进程锁，yield ``True``=持锁 / ``False``=未取到（调用方走无锁 fail-open）。
    开关关闭或 Redis 不可用时恒 ``False``。
    """
    if not settings.cache_lock_enabled:
        yield False
        return
    client = await redis_client.get_redis(key)
    if client is None:
        yield False
        return

    lock_key = _lock_key(key)
    token = uuid.uuid4().hex
    ttl = max(1, int(settings.cache_lock_ttl_s))
    acquired = False
    try:
        result = await _acquire(client, lock_key, token, ttl)
        if result is False:
            result = await _wait_for_lock(client, lock_key, token, ttl)
        acquired = result is True
        cache_lock_total.labels(
            "acquired" if acquired else "error" if result is None else "timeout"
        ).inc()
        yield acquired
    finally:
        if acquired:
            await _release(client, lock_key, token)
