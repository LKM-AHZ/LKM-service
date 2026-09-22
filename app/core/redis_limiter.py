"""基于 Redis 有序集合（ZSET）的精确滑动窗口限流器（async）。

与旧内存滑动窗口语义一致；Redis 不可用时放行（fail-open）。
"""

import logging
import uuid
from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core import redis as _redis_core

logger = logging.getLogger(__name__)

# 原子脚本：清过期 -> 判限 -> 加戳 -> 设 TTL。返回 1 放行 / 0 拦截。
# 时钟取自 Redis 服务端（TIME）而非应用侧传入：多实例共用一个 Redis 时，各实例本机
# 时钟的偏移（NTP 回拨/未同步）会让窗口起点不一致——慢的实例过拦、快的实例漏计。
_LUA_ALLOW_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local window = tonumber(ARGV[1])
local max_count = tonumber(ARGV[2])
local member = ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
if redis.call('ZCARD', KEYS[1]) >= max_count then
  return 0
end
redis.call('ZADD', KEYS[1], now, member)
redis.call('EXPIRE', KEYS[1], math.ceil(window))
return 1
"""

_script_sha_local: str | None = None


async def _ensure_script(redis: Redis) -> str:
    """在连接上注册脚本并缓存 SHA（每个进程首次调用一次，并发下幂等）。"""
    global _script_sha_local
    if _script_sha_local is None:
        _script_sha_local = await redis.script_load(_LUA_ALLOW_SCRIPT)
    return _script_sha_local


class RedisRateLimiter:
    """每个 key 维护一个 ZSET（score=时间戳、member=随机 UUID）。

    ``fail_open=True``（默认）时 Redis 不可用/异常即放行，适合读缓存等非安全场景；
    登录、验证码、2FA 等安全限流应传 ``fail_open=False``，Redis 故障时宁可拒绝
    （fail-close），避免暴力破解防线在依赖抖动瞬间消失。
    """

    async def check(
        self,
        key: str,
        max_count: int,
        window_seconds: float,
        *,
        fail_open: bool = True,
    ) -> bool:
        """允许继续则 True；否则 False。失败时按 *fail_open* 决定放行或拒绝。"""
        redis = await _redis_core.get_redis()
        if redis is None:
            return fail_open

        async def _eval(attempt: int = 0) -> bool:
            sha = await _ensure_script(redis)
            try:
                # 数字参数转 str 传入(redis 5.x stub 仅收 str)；Lua 内用 tonumber 还原。
                # 时间戳由脚本内的 Redis TIME 产生（见脚本注释），这里不再传本机时钟。
                # 返回值 cast 成 Awaitable[int] 以符合 5.x 的 `Awaitable[str]|str` 存根。
                awaitable = cast(
                    Awaitable[int],
                    redis.evalsha(
                        sha,
                        1,
                        key,
                        str(float(window_seconds)),
                        str(int(max_count)),
                        uuid.uuid4().hex,
                    ),
                )
                return await awaitable == 1
            except ResponseError as exc:
                if attempt == 0 and "NOSCRIPT" in str(exc).upper():
                    # 缓存的 SHA 在本进程外失效（Redis 重启 / SCRIPT FLUSH / 主从切换 /
                    # 改指其它实例）：丢弃本地缓存重载脚本再试一次，否则会被下面的兜底
                    # 当成普通异常，限流在 fail_open 下静默失效直到进程重启。
                    global _script_sha_local
                    _script_sha_local = None
                    return await _eval(1)
                raise

        try:
            return await _eval()
        except Exception:
            logger.warning(
                "redis 限流执行失败，按 fail_open=%s 处理 key=%s",
                fail_open,
                key,
                exc_info=True,
            )
            return fail_open  # 运行期异常按 fail_open 决定

    async def reset(self, key: str) -> None:
        """清除 *key*。Redis 不可用或 key 不存在时静默无操作。"""
        redis = await _redis_core.get_redis()
        if redis is None:
            return
        try:
            await redis.delete(key)
        except Exception:
            return
