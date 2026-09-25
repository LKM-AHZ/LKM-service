"""读热点缓存 Redis（模块4）：columns/articles 公开只读接口键缓存。

- **键规范**：`lkm:{env}:{prefix}:{parts...}`；parts 逐一 str，None 归并为空段。
  env 命名空间隔离 dev/prod 共用同一 Redis 时的互相污染。
- **TTL 分级**：明细长、列表短，平衡一致性与命中。
- **失效**：写路径显式失效（集合用版本号、单条目删键），TTL 仅兜底。
- **fail-open**：Redis 未启用/不可用 → 一律返回 None/直接落库，服务不挂。
- **可观测**：命中/未命中用 `lkm.cache` 的 DEBUG 级日志，配合模块0结构化日志观测命中率。
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import app.core.redis as redis_client
from app.core import logging as lkm_logging
from app.core import singleflight
from app.core.cache_lock import l2_lock
from app.core.config import settings

logger = logging.getLogger("lkm.cache")

# TTL 分级（秒）：单条目长缓存、列表短缓存
TTL_ITEM_S = 300  # 5 min：单条目（如 get_by_slug / get）
TTL_LIST_S = 60  # 1 min：列表/分页


def _cache_env() -> str:
    """当前 env 命名空间：读到即缓存，避免同一 Redis 不同环境互相污染。"""
    return settings.env or "dev"


def _escape_seg(seg: str) -> str:
    """转义分段里的分隔符与转义符本身（顺序要紧：先 % 再 |，否则转义不可逆）。"""
    return seg.replace("%", "%25").replace("|", "%7C")


def make_key(prefix: str, *parts: Any) -> str:
    """键规范：`lkm:{env}:{prefix}:{parts...}`。parts 的 None 归并为空段。

    分段先转义 ``|``/``%`` 再拼接：``|`` 本身是合法字符，不转义时 ("a|b","c") 与
    ("a","b|c") 会拼出同一个键，让一个变体读到另一个变体的缓存。None 与 "" 仍按既有约定
    归并成同一段——两者在调用点语义相同，属刻意行为。
    """
    seg = [_escape_seg("" if p is None else str(p)) for p in parts]
    return f"lkm:{_cache_env()}:{prefix}:{'|'.join(seg)}"


async def cache_get(key: str) -> Any | None:
    """读缓存；Redis 不可用/未配置 → None（fail-open 直查库）。"""
    client = await redis_client.get_redis()
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
    """写缓存；Redis 不可用静默跳过（不影响主路径）。"""
    client = await redis_client.get_redis()
    if client is None:
        return
    try:
        await client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
    except Exception:
        logger.debug("cache set skip key=%s", key)


async def cache_invalidate(*keys: str) -> None:
    """显式失效一个或多个键；Redis 不可用静默跳过。"""
    client = await redis_client.get_redis()
    if client is None:
        return
    try:
        if keys:
            await client.delete(*keys)
    except Exception:
        logger.debug("cache invalidate skip keys=%s", keys)


async def collection_version(name: str) -> str:
    """读集合版本号（用于列表键前缀，写后 bump 使旧列表立即失效）。

    未启用 Redis → 返回固定 "v0"，此时缓存键退化但 fail-open 直接落库也成立。
    """
    client = await redis_client.get_redis()
    if client is None:
        return "v0"
    try:
        val = await client.get(make_key("ver", name))
    except Exception:
        return "v0"
    return val or "v0"


async def bump_collection_version(name: str) -> None:
    """写操作后递增集合版本号，使该集合所有旧列表键失效（原子，免 SCAN）。"""
    client = await redis_client.get_redis()
    if client is None:
        return
    try:
        await client.incr(make_key("ver", name))
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
    """读缓存命中直接返回；未命中执行 loader 并回填。loader 输出需 JSON 可序列化。

    - 并发 miss 走单飞（``core.singleflight``，按引用计数自回收）：同一 key 同时仅有一个
      loader 在执行。此前这里自持 ``_flight_locks`` 锁字典，但键里含用户可控 slug 与每次
      bump 都变的版本号（见调用方 make_key），字典只增不减 → 无界内存；且 asyncio.Lock
      首次 await 会绑定事件循环，跨 loop 复用会抛错。
    - 传 ``null_ttl`` 时，loader 返回 None 会以该短 TTL 写入空值标记缓存，
      在窗口内让无效查询（如不存在的 slug/id）命中缓存而不再穿透到 DB。
      未命中缓存时返回 None，语义与不缓存一致。
    """
    cached = await cache_get(key)
    if cached is not None:
        if cached == _NULL_MARKER:  # 空值标记：视为不存在，短窗口内不调 loader
            return None  # ty: ignore[invalid-return-type]  # 空值标记：业务上"不存在"，返回 None
        return cached

    async def _load_and_fill() -> T:
        # 进程内已由 singleflight 收敛；这里再叠**跨进程** L2 锁（B4），使多副本部署下
        # 也只有持锁实例回填 DB 结果（蓝图 §5.6 的 double-check）。锁不可用/等锁超时
        # 一律 fail-open：照常回填，只是可能多回填一次。
        async with l2_lock(key) as held:
            # 等锁期间可能已有实例回填（或在无锁模式下并发回填）：先重读
            cached2 = await cache_get(key)
            if cached2 is not None:
                if cached2 == _NULL_MARKER:
                    return None  # ty: ignore[invalid-return-type]  # 空值标记：业务上"不存在"
                return cached2
            value = await loader()
            if held:
                # 仅持锁者回填：避免等锁超时者用（可能更旧的）结果覆盖持锁者的新值
                if value is not None:
                    await cache_set(key, value, ttl_seconds)
                elif null_ttl is not None:
                    await cache_set(key, _NULL_MARKER, null_ttl)
            return value

    return await singleflight.run(key, _load_and_fill)
