"""跨进程布隆过滤器（Redis bitmap）：挡「非法/不可能存在」的 key（蓝图 §5.6）。

蓝图 §5.6 把防穿透拆成互补两面：

- **空值缓存**（``user_cache.write_negative``）：对「合法但查无结果」的 key 显式缓存空值；
- **布隆过滤器**（本模块）：挡「非法/不可枚举」的 key 形态，白名单式拦截。

为什么用 Redis bitmap 而非进程内位数组：API 是多 worker/多副本部署，进程内位数组各持
一份、都只见过自己那部分 key，跨进程挡不住任何东西；``SETBIT``/``GETBIT`` 天然共享，且
零新依赖（复用 ``app.core.redis`` 既有客户端）。位数组大小 ``m`` 与哈希轮数 ``k`` 由
``settings.bloom_filter_capacity`` / ``bloom_filter_error_rate`` 经标准公式推导（见
:func:`_params`）。

**fail-open 是硬约束**：Redis 未配置/不可用/命令异常 → :func:`might_contain` 一律返回
``True``（= 不拦）。宁可放过一个非法 key，交给下游空值缓存/DB 兜底；也**绝不**错拦一个
合法 key——漏判只损失性能，误判直接丢正确性。

**语义边界（重要）**：本模块只提供原语，当前**不**在任何读路径做 ``might_contain`` 拦截。
布隆不支持删除，一旦某 id 先判「不存在」、随后又被真实创建，按缺失拦截会**永久**误拒该
合法 id；而「白名单式拦截非法形态」需要一份权威存量 id 全集来建过滤器，当前代码没有该
接入点（user_cache 的入参已是 ``uuid.UUID``，非法形态在 FastAPI/pydantic 边界即被 422
拦下，根本到不了缓存层）。故见 :func:`add` 的调用点说明。
"""

from __future__ import annotations

import hashlib
import math

import app.core.redis as redis_client
from app.core.cache import make_key
from app.core.config import settings


def _params(capacity: int, error_rate: float) -> tuple[int, int]:
    """容量 ``n`` / 误判率 ``p`` → ``(m, k)``。

    标准最优解：位数组 ``m = -n·ln p / (ln 2)²``、哈希轮数 ``k = (m/n)·ln 2``（≈ ``-log2 p``）。
    入参做下限收敛，避免 capacity<=0 或 p 越界时算出 0/负值把 Redis 命令弄崩。
    """
    n = max(1, capacity)
    p = min(max(error_rate, 1e-9), 0.999_999)
    m = max(1, int(-(n * math.log(p)) / (math.log(2) ** 2)) + 1)
    k = max(1, round((m / n) * math.log(2)))
    return m, k


def _bitmap_key(name: str = "ids") -> str:
    """位数组在 Redis 上的键：套用 ``lkm:{env}:bloom:*`` 命名空间，与其它缓存同源隔离。"""
    return make_key("bloom", name)


def _positions(key: str, m: int, k: int) -> list[int]:
    """把 key 映射到 k 个位下标（双重哈希 / Kirsch-Mitzenmacher，一次摘要出两组 64bit）。

    只算一次摘要再线性组合，避免实现 k 个独立哈希函数；``blake2b`` 是标准库、跨进程稳定。
    第二组取奇数：与 2 的幂取模时降低步长退化为 0/与 m 不互质的概率。
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=16).digest()
    h1 = int.from_bytes(digest[:8], "big")
    h2 = int.from_bytes(digest[8:], "big") | 1
    return [(h1 + i * h2) % m for i in range(k)]


def _enabled() -> bool:
    return bool(settings.bloom_filter_enabled)


async def add(key: str) -> bool:
    """把 key 记入过滤器；返回是否真的写入。

    Redis 未配置/不可用、开关关闭、或命令异常 → 返回 ``False``（静默 fail-open，绝不抛）。
    调用方无需因写入失败改变主流程语义。
    """
    if not _enabled():
        return False
    client = await redis_client.get_redis()
    if client is None:
        return False
    m, k = _params(settings.bloom_filter_capacity, settings.bloom_filter_error_rate)
    bitmap = _bitmap_key()
    try:
        async with client.pipeline(transaction=False) as pipe:
            for pos in _positions(key, m, k):
                pipe.setbit(bitmap, pos, 1)
            await pipe.execute()
    except Exception:
        return False
    return True


async def might_contain(key: str) -> bool:
    """key 是否**可能**在集合中。

    - 返回 ``False`` ⇒ 一定不在（k 个位全为 0）；
    - 返回 ``True`` ⇒ 可能在，或**无法判定**（Redis 不可用/开关关闭 → fail-open 不拦）。

    调用方若据此走「拒绝」分支，必须自行确认误判不会伤及合法 key（见模块 docstring 的
    语义边界）。
    """
    if not _enabled():
        return True
    client = await redis_client.get_redis()
    if client is None:
        return True
    m, k = _params(settings.bloom_filter_capacity, settings.bloom_filter_error_rate)
    bitmap = _bitmap_key()
    try:
        async with client.pipeline(transaction=False) as pipe:
            for pos in _positions(key, m, k):
                pipe.getbit(bitmap, pos)
            bits = await pipe.execute()
    except Exception:
        return True
    return all(int(bit) == 1 for bit in bits)
