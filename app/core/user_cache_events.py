"""user:snap L1 失效广播（roadmap §5.6）：跨实例同步删本地 L1。

L1 是各进程私有的内存缓存，失效必须广播才能让**其他** worker 也删掉本地副本。本模块
用 Redis pub/sub 承载：

- **发布方**（worker 进程消费 auth 变更事件 → ``user_cache.invalidate_user_snap``）在完成
  L2 ``INCR epoch + DEL`` 后 publish 被失效的 L2 key。
- **订阅方**（API 进程，``main.lifespan`` 启动常驻 task）收到后**只删本地 L1**，**不 DEL L2**
  ——L2 已由发布方删除；订阅方再删会误删其他实例刚回填的新值，只增 miss 不增正确性。

fail-open：Redis 未配置/不可用静默跳过；pub/sub 非持久，丢广播由 L1 短 TTL（默认 10s）
兜底自愈——因此 L1 TTL 就是陈旧窗口上界。订阅 task 模式与 ``app/ws/manager.py`` 一致
（常驻 + 退避重连 + cancel 收尾）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from app.core import local_cache
from app.core import redis as redis_client
from app.core.cache import make_key

logger = logging.getLogger("lkm.user_cache.events")

_sub_task: asyncio.Task[None] | None = None
_start_lock = asyncio.Lock()


def _channel() -> str:
    """失效广播频道（复用 env 命名空间，隔离 dev/prod 共用 Redis）。"""
    return make_key("cache:l1:invalidate")


async def publish_invalidate(key: str) -> None:
    """广播「某 L2 key 已失效」；Redis 不可用/发布失败静默（靠 L1 TTL 自愈）。"""
    redis = await redis_client.get_redis()
    if redis is None:
        return
    try:
        await redis.publish(_channel(), key)
    except Exception:
        logger.debug("l1 invalidate publish skip key=%s", key)


async def start() -> None:
    """幂等启动常驻订阅 task（已运行则跳过）。"""
    global _sub_task
    if _sub_task is not None and not _sub_task.done():
        return
    async with _start_lock:
        if _sub_task is not None and not _sub_task.done():
            return
        _sub_task = asyncio.create_task(_sub_loop())


async def _sub_loop() -> None:
    """常驻：subscribe 失效频道 → 删本地 L1。Redis 未就绪/异常均退避重连。"""
    while True:
        redis = await redis_client.get_redis()
        if redis is None:
            await asyncio.sleep(1)
            continue
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(_channel())
            while True:
                msg: dict[str, Any] | None = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg is None or msg.get("type") != "message":
                    continue
                key = msg.get("data")
                if isinstance(key, bytes):
                    key = key.decode()
                if isinstance(key, str) and key:
                    local_cache.l1_delete(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            with suppress(Exception):
                await pubsub.aclose()
            await asyncio.sleep(1)


async def stop() -> None:
    """收尾：取消订阅 task（幂等）。须在 redis 客户端关闭前调用。"""
    global _sub_task
    task, _sub_task = _sub_task, None
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
