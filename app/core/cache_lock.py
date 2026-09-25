"""跨进程缓存互斥锁（蓝图 §5.6 的 L2 double-check 击穿防护）。

**为什么需要**：``core/singleflight.py`` 只收敛**单进程**内的并发 miss；多副本部署下 N 个
进程会各自回填一次 L2，DB 压力 × N。本模块提供跨进程互斥，让「仅持锁实例回填」成立。

**失败模式全部 fail-open**（宁可多查一次 DB，不可让读阻塞或抛错）：

- 持锁者崩溃 → 锁的 TTL 自动过期，无需人工清理（无 TTL 的锁正是死锁的来源）；
- 等锁超时（``cache_lock_wait_ms``）→ 放弃等待、走无锁直读，**不制造死锁**；记
  ``cache_lock_total{result=timeout}`` 以便观察。
- 释放用 ``WATCH`` + token 比对 + ``MULTI/DEL`` 乐观锁：**刻意不用 Lua**——单测环境的
  fakeredis 没有脚本引擎，``eval`` 会抛；若被 suppress 包住就成了「锁永远删不掉」的静默
  假绿（本仓 2026-09-22 在 ``migration_lock`` 踩过同一坑）。

与 ``redis.set(nx=True)`` 的既有先例（``auth/user_dim_sync.py``、``db/migration_lock.py``）
同款原语，差别在本模块是**短 TTL + 可放弃等待**的读路径锁，不追求严格互斥。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core import redis as redis_client
from app.core.config import settings
from app.core.metrics import cache_lock_total

logger = logging.getLogger("lkm.cache.lock")

_LOCK_PREFIX = "lkm:lock:"
_POLL_INTERVAL_S = 0.02


def _lock_key(key: str) -> str:
    """锁键由缓存键派生（缓存键已含 env 命名空间，故此处不再重复加）。"""
    return f"{_LOCK_PREFIX}{key}"


async def _acquire(client: Any, lock_key: str, token: str, ttl: int) -> bool:
    try:
        return bool(await client.set(lock_key, token, nx=True, ex=ttl))
    except Exception:
        logger.debug("cache lock acquire skip key=%s", lock_key)
        return False


async def _wait_for_lock(client: Any, lock_key: str, token: str, ttl: int) -> bool:
    """在 ``cache_lock_wait_ms`` 内轮询重试（等锁期间持锁者通常已完成回填）。"""
    deadline = settings.cache_lock_wait_ms / 1000.0
    waited = 0.0
    while waited < deadline:
        await asyncio.sleep(_POLL_INTERVAL_S)
        waited += _POLL_INTERVAL_S
        if await _acquire(client, lock_key, token, ttl):
            return True
    return False


async def _release(client: Any, lock_key: str, token: str) -> None:
    """只删**自己持有的**锁：WATCH + token 比对 + MULTI/DEL。

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
async def l2_lock(key: str) -> AsyncIterator[bool]:
    """尝试获取跨进程锁，yield ``True``=持锁 / ``False``=未取到（调用方走无锁 fail-open）。

    开关关闭或 Redis 不可用时恒 ``False``——此时行为与引入本模块前完全一致。
    """
    if not settings.cache_lock_enabled:
        yield False
        return
    client = await redis_client.get_redis()
    if client is None:
        yield False
        return

    lock_key = _lock_key(key)
    token = uuid.uuid4().hex
    ttl = max(1, int(settings.cache_lock_ttl_s))
    acquired = False
    try:
        acquired = await _acquire(client, lock_key, token, ttl) or await _wait_for_lock(
            client, lock_key, token, ttl
        )
        cache_lock_total.labels("acquired" if acquired else "timeout").inc()
        yield acquired
    finally:
        if acquired:
            await _release(client, lock_key, token)
