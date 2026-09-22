"""Redis 迁移锁：串行化多 worker 并发的 Alembic upgrade。

业务库与 auth 库是两条独立迁移链，各自用不同 key 上锁（互不阻塞），故锁工具从
``app/db/init_db.py`` 抽出为共享模块，由两侧调用方自行传入 key。

Redis 不可用（未配置/宕机）→ fail-open 不设锁直接跑（幂等 no-op）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, cast

from redis.exceptions import WatchError

logger = logging.getLogger(__name__)

MIGRATION_LOCK_TTL = 120  # 秒：迁移超时上限后锁自动过期
MIGRATION_LOCK_POLL = 0.3  # 轮询间隔
# 等待上限须 >= TTL：锁的最长寿命就是 TTL，等得比它短意味着只要对方迁移稍慢，
# 本 worker 就放弃等待并**并行**起第二条 alembic upgrade —— 恰好在真正争用的场景
# 绕过本模块要提供的串行化（原值 8s 远小于 120s 的 TTL）。
MIGRATION_LOCK_WAIT = MIGRATION_LOCK_TTL + MIGRATION_LOCK_POLL

# 本进程实际持有锁时写入的 token（按 key）。释放时比对 token 才删，防误删别人的锁。
_tokens: dict[str, str] = {}

# 持有期间的后台续期任务（key → (task, stop_event)）。MIGRATION_LOCK_TTL 是锁的最长寿命，
# 而真实迁移经 asyncio.to_thread 跑、耗时不可控：不续期时慢迁移（大表/锁等待）会中途丢锁，
# 下个 worker 等满 WAIT 后就会并行起第二条 alembic upgrade，正是本模块要串行化掉的场景。
_renewers: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}

_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)


async def _renew_loop(
    client: Any, key: str, token: str, stop: asyncio.Event
) -> None:
    """按 TTL 的一半周期续期；只续自己的 token（compare-and-expire）。"""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=MIGRATION_LOCK_TTL / 2)
            return  # 收到停止信号
        except TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await cast(
                Awaitable[int],
                client.eval(_RENEW_LUA, 1, key, token, MIGRATION_LOCK_TTL),
            )
        except Exception:
            # 续期失败不抛出（不能让后台任务的异常影响迁移主流程），但必须留痕：
            # 持续失败意味着锁即将到期
            logger.warning("迁移锁续期失败 key=%s（锁可能到期）", key, exc_info=True)


def _as_text(value: Any) -> str | None:
    """统一 Redis 返回值为 str：decode_responses=False 的客户端（如测试里的 fakeredis）
    返回 bytes，直接与 str token 比较会恒不相等（锁就永远删不掉）。"""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


async def _delete_if_owner(client: Any, key: str, token: str) -> None:
    """compare-and-delete：值仍是自己的 token 才删，防误删别人的锁。

    刻意用 WATCH/MULTI 乐观锁而非 Lua ``eval``：fakeredis（单测用的假 Redis，本机未装
    lupa）没有 Lua 引擎，eval 会抛错并被上层的 suppress 吞掉——锁永远删不掉，测试里
    「迁移结束锁已释放」的断言直接红。WATCH/MULTI 在真假 Redis 上都可用，也与 outbox
    relay 的租约释放同一范式。
    """
    async with client.pipeline(transaction=True) as pipe:
        for _ in range(3):
            try:
                await pipe.watch(key)
                if _as_text(await pipe.get(key)) != token:
                    await pipe.reset()
                    return  # 已被接管/已过期：绝非自己的锁，不删
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
                return
            except WatchError:
                await pipe.reset()  # 并发改写，重试乐观锁


def _start_renewer(client: Any, key: str, token: str) -> None:
    stop = asyncio.Event()
    _renewers[key] = (asyncio.create_task(_renew_loop(client, key, token, stop)), stop)


async def _stop_renewer(key: str) -> None:
    entry = _renewers.pop(key, None)
    if entry is None:
        return
    task, stop = entry
    stop.set()
    task.cancel()
    with suppress(Exception, asyncio.CancelledError):
        await task


async def acquire_migration_lock(key: str) -> bool:
    """用 Redis SET NX 抢迁移锁；未配置/失败返回 False（fail-open 不设锁）。

    锁值不用固定字面量而用本次获取的随机 token：固定值时任何 worker 都能删掉
    别人的锁（A 的锁超时过期后 B 拿到，A 结束时删掉 B 的锁，C 就能与 B 并行迁移）。
    """
    from app.core import redis as redis_client

    client = await redis_client.get_redis()
    if client is None:
        return False
    token = uuid.uuid4().hex
    try:
        if bool(await client.set(key, token, nx=True, ex=MIGRATION_LOCK_TTL)):
            _tokens[key] = token
            _start_renewer(client, key, token)
            return True
        # 拿不到 → 有别的 worker 在迁移：轮询等待其释放
        waited = 0.0
        while waited < MIGRATION_LOCK_WAIT:
            await asyncio.sleep(MIGRATION_LOCK_POLL)
            waited += MIGRATION_LOCK_POLL
            # 对方已释放/锁已过期 → SET NX 成功即由自己接管
            if bool(await client.set(key, token, nx=True, ex=MIGRATION_LOCK_TTL)):
                _tokens[key] = token
                _start_renewer(client, key, token)
                return True
        return False  # 等待超时：照常跑（幂等 no-op）
    except Exception as exc:
        # fail-open 不变，但必须留痕：Redis 故障会静默关掉迁移串行化
        # （多 worker 并发 upgrade 是真实风险，不能只表现为「什么都没发生」）
        logger.warning("迁移锁获取失败 key=%s，fail-open 不设锁：%s", key, exc)
        return False


async def release_migration_lock(held: bool, key: str) -> None:
    if not held:
        return
    from app.core import redis as redis_client

    # 先停续期再删锁：否则续期可能在删除之后又把 key 续上（留下永不释放的锁）
    await _stop_renewer(key)
    client = await redis_client.get_redis()
    token = _tokens.pop(key, None)
    if client is None or token is None:
        return
    with suppress(Exception):
        await _delete_if_owner(client, key, token)
