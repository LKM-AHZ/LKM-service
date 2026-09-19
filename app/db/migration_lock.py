"""Redis 迁移锁：串行化多 worker 并发的 Alembic upgrade。

业务库与 auth 库是两条独立迁移链，各自用不同 key 上锁（互不阻塞），故锁工具从
``app/db/init_db.py`` 抽出为共享模块，由两侧调用方自行传入 key。

Redis 不可用（未配置/宕机）→ fail-open 不设锁直接跑（幂等 no-op）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

MIGRATION_LOCK_TTL = 120  # 秒：迁移超时上限后锁自动过期
MIGRATION_LOCK_WAIT = 8  # 秒：拿不到锁时最多等待的时长
MIGRATION_LOCK_POLL = 0.3  # 轮询间隔


async def acquire_migration_lock(key: str) -> bool:
    """用 Redis SET NX 抢迁移锁；未配置/失败返回 False（fail-open 不设锁）。"""
    from app.core import redis as redis_client

    client = await redis_client.get_redis()
    if client is None:
        return False
    try:
        ok = bool(await client.set(key, "1", nx=True, ex=MIGRATION_LOCK_TTL))
        if ok:
            return True
        # 拿不到 → 有别的 worker 在迁移：轮询等待其释放
        waited = 0.0
        while waited < MIGRATION_LOCK_WAIT:
            await asyncio.sleep(MIGRATION_LOCK_POLL)
            waited += MIGRATION_LOCK_POLL
            # 对方已释放并成功重新抢占（lock 已过期）→ 自己来迁
            gone = bool(await client.get(key)) is False
            if gone and bool(
                await client.set(key, "1", nx=True, ex=MIGRATION_LOCK_TTL)
            ):
                return True
        return False  # 等待超时：照常跑（幂等 no-op）
    except Exception:
        return False  # Redis 异常 → fail-open


async def release_migration_lock(held: bool, key: str) -> None:
    if not held:
        return
    from app.core import redis as redis_client

    client = await redis_client.get_redis()
    if client is None:
        return
    with suppress(Exception):
        await client.delete(key)
