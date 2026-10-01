"""
Redis 接入层：懒初始化异步客户端，未配置/不可用时返回 None（fail-open 前提）。
支持**双 L2 后端并行 + 灰度**：按 key 前缀把请求路由到主后端（``redis_url``）或第二后端
（``redis_url_secondary``，如 Dragonfly）。``get_redis(key)`` 给了 key 就据此路由，未给
（health 探针、pub/sub 等无 key 场景）用主后端。
**为什么按前缀路由即可保证一致**：key 规范是 ``lkm:{env}:{prefix}:...``（``core.cache.make_key``），
同一前缀恒定落同一后端，故缓存/锁/版本号/epoch 不会跨后端分裂，SCAN/MGET/WATCH 多键也都落在
单一后端内、无需合并。灰度 = 把某个域的前缀加进 ``redis_secondary_prefixes``。
"""

import asyncio
import logging
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis

from core.config import settings
from core.secrets import reveal

logger = logging.getLogger(__name__)

_client: Redis | None = None  # 主后端
_client_pool: Any = None  # 底层池引用（测试替换为 fakeredis）
_secondary_client: Redis | None = None
_secondary_pool: Any = None
_LOCK = asyncio.Lock()
_PING_TIMEOUT = 0.2  # 秒


def _is_enabled() -> bool:
    """未配置 redis_url 即视为关闭。"""
    return bool(reveal(settings.redis_url))


def _secondary_enabled() -> bool:
    """第二后端是否已配置（**未配置 = 不启用并行**，行为与单后端完全一致）。"""
    return bool(reveal(settings.redis_url_secondary))


def secondary_prefixes() -> tuple[str, ...]:
    """解析配置里的路由前缀列表（逗号分隔）。"""
    raw = settings.redis_secondary_prefixes or ""
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _route_target(key: str) -> str:
    """把 key 归一为用于前缀匹配的串：剥掉 ``lkm:{env}:`` 命名空间头（裸键原样返回）。"""
    head = f"lkm:{settings.env or 'dev'}:"
    return key[len(head):] if key.startswith(head) else key


def _matches(target: str, prefix: str) -> bool:
    """
    ``target`` 是否落在 ``prefix`` 段下 —— 必须**整段**命中（后随 ``:`` 或结束）。
    要求整段命中是为避免 ``ip`` 误配 ``ipfoo`` 这类同词头异前缀。
    """
    if not target.startswith(prefix):
        return False
    rest = target[len(prefix):]
    return rest == "" or rest.startswith(":")


def is_secondary(key: str | None) -> bool:
    """该 key 是否路由到第二后端（供诊断/测试观测路由决策）。"""
    if key is None or not _secondary_enabled():
        return False
    target = _route_target(key)
    return any(_matches(target, p) for p in secondary_prefixes())


def is_enabled() -> bool:
    """主后端是否已配置（公开只读判断；供 L1 缓存等「Redis 关闭则整体关闭」的 gate 复用）。"""
    return _is_enabled()


def secondary_configured() -> bool:
    """
    第二后端是否已配置（供 health 等多后端探针判断「应当有几个后端」）。
    注意与 :func:`all_clients` 的区别：后者只返回**当前可用**的后端，故「配置了但连不上」
    不会出现在那里——health 需要拿本函数算出的应有数量与之比对才能发现降级。
    """
    return _secondary_enabled()


async def _connect(url: str) -> Any:
    """建客户端 + PING 探测；不可用返回 None（fail-open）。"""
    pool: Any = None
    try:
        pool = Redis.from_url(
            url,
            decode_responses=True,
            # 每次命令的 socket 超时：Redis 半挂（网络黑洞）时命令最多等0.5s 即抛错
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        # 探测：PING 在极短超时内通过才视为可用。不用 assert 表达——`python -O`
        # 会整句删除（含 wait_for），探测连同超时一起消失，不可用的 Redis 会被
        # 当成 "可用" 缓存进 _client，与 fail-open 契约相反。
        try:
            pong = await asyncio.wait_for(pool.ping(), _PING_TIMEOUT)
            if not pong:
                raise RuntimeError("redis ping 返回假值")
        except Exception as exc:
            # 必须留痕：否则 URL 配错/DNS/TLS 失败/宕机都表现为「无 Redis」，限流被静默
            # 关闭而无从排查。fail-open 返回值不变。
            logger.warning("redis ping 失败，降级为不可用: %s", exc)
            # aclose 自身失败也要继续走降级路径，否则异常冒到外层只置空引用、池未释放
            with suppress(Exception):
                await pool.aclose()
            return None
        return pool
    except Exception as exc:
        # 初始化或连接阶段任何异常都降级为 None
        logger.warning("redis 客户端初始化失败，降级为不可用: %s", exc)
        return None


async def _get_secondary_client() -> Redis | None:
    """第二后端客户端（未配置返回 None）。"""
    global _secondary_client, _secondary_pool
    if not _secondary_enabled():
        return None
    if _secondary_client is not None:
        return _secondary_client
    async with _LOCK:
        if _secondary_client is None:
            _secondary_pool = await _connect(reveal(settings.redis_url_secondary))
            _secondary_client = _secondary_pool
        return _secondary_client


async def get_redis(key: str | None = None) -> Redis | None:
    """
    返回可用的 Redis 客户端；未启用或连接/探测失败返回 None。
    - ``key`` 给了且前缀命中 ``redis_secondary_prefixes`` → 第二后端；
    - 否则（含 ``key=None`` 的无 key 场景，如 health 探针、pub/sub 频道）→ 主后端。
    失败时返回 None（fail-open），由调用方据此放行/回源。每次调用从共享单例返回。
    """
    global _client, _client_pool

    if is_secondary(key):
        return await _get_secondary_client()

    if not _is_enabled():
        return None
    if _client is not None:
        return _client
    async with _LOCK:
        if _client is None:
            _client_pool = await _connect(reveal(settings.redis_url))
            _client = _client_pool
        return _client


async def all_clients() -> list[tuple[str, Redis]]:
    """
    全部**已启用且可用**的后端 ``[(label, client), ...]``，供 health 等多后端探针遍历。
    顺序为主后端在前；未配置的后端不出现。任一后端不可用即从列表缺席——调用方据「列表长度
    是否覆盖已配置的后端」判定整体健康。
    """
    out: list[tuple[str, Redis]] = []
    primary = await get_redis(None)
    if primary is not None:
        out.append(("primary", primary))
    secondary = await _get_secondary_client()
    if secondary is not None:
        out.append(("secondary", secondary))
    return out


async def close_redis() -> None:
    """
    关闭并清空两个后端的单例（应用收尾调用）。幂等。
    与 ``get_redis`` 共用 ``_LOCK``：否则并发 ``get_redis`` 可能拿到一个正在 ``aclose``
    的池（连接已断），或在收尾清空后又新建一个绑在已关闭事件循环上的 client。
    """
    global _client, _client_pool, _secondary_client, _secondary_pool
    async with _LOCK:
        for pool_ref in (_client_pool, _secondary_pool):
            if pool_ref is not None:
                with suppress(Exception):
                    await pool_ref.aclose()
        _client = None
        _client_pool = None
        _secondary_client = None
        _secondary_pool = None
