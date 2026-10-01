"""
读热点缓存 Redis：columns/articles 公开只读接口键缓存。
- **键规范**：`lkm:{env}:{prefix}:{parts...}`；parts 逐一 str，None 归并为空段。
  env 命名空间隔离 dev/prod 共用同一 Redis 时的互相污染。
- **TTL 分级**：明细长、列表短，平衡一致性与命中。
- **失效**：集合用版本号；单条目原子更新短期失效代次并删键，阻止在途旧回填。
- **fail-open**：Redis 未启用/不可用 → 一律返回 None/直接落库，服务不挂。
- **可观测**：命中/未命中用 `lkm.cache` 的 DEBUG 级日志，配合结构化日志观测命中率。
"""

import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis import WatchError

import core.redis as redis_client
from core import logging as lkm_logging
from core import singleflight
from core.cache_lock import l2_lock
from core.config import settings

logger = logging.getLogger("lkm.cache")

# TTL 分级（秒）：单条目长缓存、列表短缓存
TTL_ITEM_S = 300
TTL_LIST_S = 60

# TTL 随机扰动幅度。同批写入的 key 若 TTL 完全相同，会在同一刻集体过期，缓存层瞬间全量回源打 DB/AUTH。
# 写入时按此比例摊开过期时刻，每个 key 只算一次。
# 只适用于**缓存对象**的 TTL。
_TTL_JITTER_RATIO = 0.3
# 显式失效后短期保留代次，拦下失效前已开始的 loader；超过该时长的加载结果不回填。
_INVALIDATION_GUARD_TTL_S = 60


def _invalidation_key(key: str) -> str:
    return f"lkm:cache:invalidated:{key}"


async def _set_if_not_invalidated(
    client: Any,
    key: str,
    value: Any,
    ttl_seconds: int,
    token: str | bytes | None,
    started_at: float,
) -> None:
    """只在加载期间目标键与失效代次均未变化时回填。"""
    if time.monotonic() - started_at >= _INVALIDATION_GUARD_TTL_S:
        return
    try:
        serialized = json.dumps(value, ensure_ascii=False)
        async with client.pipeline(transaction=True) as pipe:
            await pipe.watch(key, _invalidation_key(key))
            current_token = await pipe.get(_invalidation_key(key))
            current_value = await pipe.get(key)
            if current_token != token:
                await pipe.unwatch()
                return
            if current_value is not None:
                try:
                    existing = json.loads(current_value)
                except Exception:
                    existing = None
                if existing is not None:
                    await pipe.unwatch()
                    return
            pipe.multi()
            pipe.set(key, serialized, ex=jitter_ttl(ttl_seconds))
            await pipe.execute()
    except WatchError:
        # 失效或其他回填恰好发生在 WATCH 之后；当前 loader 的结果不再写入。
        return
    except Exception:
        logger.debug("cache guarded set skip key=%s", key, exc_info=True)


def jitter_ttl(base_seconds: float, *, lower_only: bool = False) -> int:
    """
    给缓存 TTL 加随机扰动，返回整数秒。
    - 默认 ±30%（`base × [0.7, 1.3)`）。
    - 结果至少 1 秒：Redis 的 ``ex`` 不接受 0/负值。
    """
    if base_seconds <= 0:
        return 1
    if lower_only:
        factor = 1.0 - _TTL_JITTER_RATIO * random.random()
    else:
        factor = 1.0 - _TTL_JITTER_RATIO + 2 * _TTL_JITTER_RATIO * random.random()
    return max(1, int(base_seconds * factor))


def _cache_env() -> str:
    """当前 env 命名空间：读到即缓存，避免同一 Redis 不同环境互相污染。"""
    return settings.env or "dev"


def _escape_seg(seg: str) -> str:
    """转义分段里的分隔符与转义符本身（顺序要紧：先 % 再 |，否则转义不可逆）。"""
    return seg.replace("%", "%25").replace("|", "%7C")


def make_key(prefix: str, *parts: Any) -> str:
    """
    键规范：`lkm:{env}:{prefix}:{parts...}`。parts 的 None 归并为空段。
    分段先转义 ``|``/``%`` 再拼接：``|`` 本身是合法字符，不转义时 ("a|b","c") 与
    ("a","b|c") 会拼出同一个键，让一个变体读到另一个变体的缓存。None 与 "" 仍按既有约定
    归并成同一段——两者在调用点语义相同，属刻意行为。
    """
    seg = [_escape_seg("" if p is None else str(p)) for p in parts]
    return f"lkm:{_cache_env()}:{prefix}:{'|'.join(seg)}"


async def cache_get(key: str) -> Any | None:
    """读缓存；Redis 不可用/未配置 → None（fail-open 直查库）。"""
    client = await redis_client.get_redis(key)
    if client is None:
        return None
    request_id = lkm_logging.get_request_id()
    try:
        raw = await client.get(key)
    except Exception:
        logger.debug("cache get fail-open key=%s req=%s", key, request_id)
        return None
    if raw is None:
        logger.debug("cache miss key=%s req=%s", key, request_id)
        return None
    logger.debug("cache hit key=%s req=%s", key, request_id)
    try:
        return json.loads(raw)
    except Exception:
        return None


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """
    写缓存；Redis 不可用静默跳过（不影响主路径）。
    入参 ``ttl_seconds`` 是**基准**值：实际 `ex` 经 :func:`jitter_ttl` 加随机扰动后再落盘。
    故本函数是所有 ``cached_read`` 类缓存 TTL 的公共收口点。
    """
    client = await redis_client.get_redis(key)
    if client is None:
        return
    try:
        await client.set(
            key, json.dumps(value, ensure_ascii=False), ex=jitter_ttl(ttl_seconds)
        )
    except Exception:
        logger.debug("cache set skip key=%s", key)


async def cache_invalidate(*keys: str) -> None:
    """按后端原子更新失效代次并删除缓存键；Redis 不可用静默跳过。"""
    if not keys:
        return
    # 一次调用可能包含不同前缀（例如 following 与 board），灰度路由时分属不同后端。
    groups: dict[bool, list[str]] = {}
    for key in keys:
        groups.setdefault(redis_client.is_secondary(key), []).append(key)
    for group in groups.values():
        client = await redis_client.get_redis(group[0])
        if client is None:
            continue
        try:
            async with client.pipeline(transaction=True) as pipe:
                pipe.multi()
                for key in group:
                    pipe.set(
                        _invalidation_key(key),
                        uuid.uuid4().hex,
                        ex=_INVALIDATION_GUARD_TTL_S,
                    )
                pipe.delete(*group)
                await pipe.execute()
        except Exception:
            logger.debug("cache invalidate skip keys=%s", group)


async def collection_version(name: str) -> str:
    """
    读集合版本号（用于列表键前缀，写后 bump 使旧列表立即失效）。
    未启用 Redis → 返回固定 "v0"，此时缓存键退化但 fail-open 直接落库也成立。
    """
    # 版本号按 ver 前缀路由；读写必须使用同一个键和后端。
    client = await redis_client.get_redis(make_key("ver", name))
    if client is None:
        return "v0"
    try:
        val = await client.get(make_key("ver", name))
        if not val:
            return "v0"
        return val.decode() if isinstance(val, bytes) else val
    except Exception:
        return "v0"


async def bump_collection_version(name: str) -> None:
    """写操作后递增集合版本号，使该集合所有旧列表键失效（原子，免 SCAN）。"""
    client = await redis_client.get_redis(make_key("ver", name))
    if client is None:
        return
    try:
        await client.incr(make_key("ver", name))  # ty: ignore[invalid-await]  # Redis async API 的类型误报
    except Exception:
        logger.debug("bump version skip name=%s", name)


# 空值缓存标记：loader 返回 None（业务上不存在）时写入该标记 + 短 TTL，读取端据此
# 在窗口内直接返回 None 而不反复调 loader，防御无效 slug/id 的缓存穿透。
_NULL_MARKER = "\x00__CACHE_NULL__"


async def cached_read[T](
    key: str,
    ttl_seconds: int,
    loader: Callable[[], Awaitable[T]],
    null_ttl: int | None = None,
) -> T:
    """
    读缓存命中直接返回；未命中执行 loader 并回填。loader 输出需 JSON 可序列化。
    - 并发 miss 走单飞（``core.singleflight``，按引用计数自回收）：同一 key 同时仅有一个
      loader 在执行。此前这里自持 ``_flight_locks`` 锁字典，但键里含用户可控 slug 与每次
      bump 都变的版本号（见调用方 make_key），字典只增不减 → 无界内存；且 asyncio.Lock
      首次 await 会绑定事件循环，跨 loop 复用会抛错。
    - 传 ``null_ttl`` 时，loader 返回 None 会以该短 TTL 写入空值标记缓存，
      在窗口内让无效查询（如不存在的 slug/id）命中缓存而不再穿透到 DB。
      未命中缓存时返回 None，语义与不缓存一致。
    - 显式失效在 Redis 留 60 秒短期代次；loader 若跨过失效，或加载过久，放弃回填，
      防止在途旧读取把已失效的明细重新写回缓存。
    """
    cached = await cache_get(key)
    if cached is not None:
        if cached == _NULL_MARKER:  # 空值标记：视为不存在，短窗口内不调 loader
            return None  # ty: ignore[invalid-return-type]  # 空值标记：业务上"不存在"，返回 None
        return cached

    async def _load_and_fill() -> T:
        # 进程内已由 singleflight 收敛；这里再叠**跨进程** L2 锁，使多副本部署下也只有持锁实例回填 DB 结果。
        # 等锁超时/Redis 锁命令失败时照常加载，但不回填，避免覆盖持锁者结果。
        # 主动关闭跨进程锁时仍须回填，否则每次请求都会穿透到 loader。
        async with l2_lock(key) as held:
            # 等锁期间可能已有实例回填（或在无锁模式下并发回填）：先重读
            cached2 = await cache_get(key)
            if cached2 is not None:
                if cached2 == _NULL_MARKER:
                    return None  # ty: ignore[invalid-return-type]  # 空值标记：业务上"不存在"
                return cached2
            can_fill = held or not settings.cache_lock_enabled
            client = await redis_client.get_redis(key) if can_fill else None
            token: str | bytes | None = None
            guard_ok = False
            if client is not None:
                try:
                    token = await client.get(_invalidation_key(key))
                    guard_ok = True
                except Exception:
                    logger.debug("cache invalidation guard read skip key=%s", key)
            started_at = time.monotonic()
            value = await loader()
            if can_fill and guard_ok and client is not None:
                # 开锁时仅持锁者回填；关闭锁时缓存仍正常写入。
                if value is not None:
                    await _set_if_not_invalidated(
                        client, key, value, ttl_seconds, token, started_at
                    )
                elif null_ttl is not None:
                    await _set_if_not_invalidated(
                        client, key, _NULL_MARKER, null_ttl, token, started_at
                    )
            return value

    return await singleflight.run(key, _load_and_fill)
