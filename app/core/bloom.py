"""跨进程布隆过滤器（Redis bitmap）：挡「不可能存在」的 key（蓝图 §5.6）。

蓝图 §5.6 把防穿透拆成互补两面：

- **空值缓存**（``user_cache.write_negative``）：对「合法但查无结果」的 key 显式缓存空值；
- **布隆过滤器**（本模块）：挡「不可能存在」的 key，白名单式拦截。

为什么用 Redis bitmap 而非进程内位数组：API 是多 worker/多副本部署，进程内位数组各持
一份、都只见过自己那部分 key，跨进程挡不住任何东西；``SETBIT``/``GETBIT`` 天然共享，且
零新依赖（复用 ``app.core.redis`` 既有客户端）。位数组大小 ``m`` 与哈希轮数 ``k`` 由
``settings.bloom_filter_capacity`` / ``bloom_filter_error_rate`` 经标准公式推导（见
:func:`_params`）。

**fail-open 是硬约束**：Redis 未配置/不可用/命令异常 → 判定一律返回「无法判定」（不拦）。
宁可放过一个不该存在的 key，交给下游空值缓存/DB 兜底；也**绝不**错拦一个合法 key——漏判
只损失性能，误判直接丢正确性。

**白名单语义与「只增不重建」**：本模块现在服务的唯一用途是 ``user:snap`` 的 id 白名单——
位图里是**全部合法 user id**，``definitely_absent(key) == True`` ⇒ 该 id 从未存在过。

- 布隆无假阴性 ⇔ 每个合法 id 都被 ``add`` 过；因此**完整性是正确性前提**，靠「全量预热
  （:func:`add_many`）+ 建号即 add + 每日重跑」保证。
- 布隆**不可删**，但这里**不需要重建**：已删除用户的 id 留在位图里无害——``might_contain``
  返 ``True`` → 落回真实查找 → 由负值缓存兜。故只做**单调追加**，没有「重建窗口内新用户
  被误拒」的坑，也没有双缓冲/换键的一致性复杂度。
- **门禁**：只有 :func:`mark_seeded` 打上「已预热」标记后才允许据布隆拒绝（:func:`definitely_absent`）。
  未预热、预热任务停摆到标记过期、Redis 不可用 → 都退回「不拦」。这是安全方向：整段不生效，
  好过在未证实完整的位图上拒绝。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

import app.core.redis as redis_client
from app.core.cache import make_key
from app.core.config import settings

# 位图名字：当前唯一用途是 user id 白名单（跨 AUTH 读路径的防穿透）。
_BITMAP_NAME = "user_ids"
# 预热写入的分块大小：一个 pipeline 最多压这么多 key（× k 条 SETBIT），避免单次命令体过大。
_ADD_CHUNK = 1000


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


def _bitmap_key(name: str = _BITMAP_NAME) -> str:
    """位数组在 Redis 上的键：套用 ``lkm:{env}:bloom:*`` 命名空间，与其它缓存同源隔离。"""
    return make_key("bloom", name)


def _seeded_key() -> str:
    """「已预热」门禁标记的键（与位图同前缀，便于一起巡检/清理）。"""
    return make_key("bloom", _BITMAP_NAME, "seeded")


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
    client = await redis_client.get_redis(_bitmap_key())
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


async def add_many(keys: Sequence[str]) -> int:
    """批量记入（预热/回填用）；返回成功写入的 key 数。

    分块 pipeline 写入，幂等（重复 add 同一 key 只是重写同样的位）。任何一块异常即停，
    返回已写入数——调用方据此判断预热是否完整（不完整就不该 :func:`mark_seeded`）。
    """
    if not keys or not _enabled():
        return 0
    client = await redis_client.get_redis(_bitmap_key())
    if client is None:
        return 0
    m, k = _params(settings.bloom_filter_capacity, settings.bloom_filter_error_rate)
    bitmap = _bitmap_key()
    written = 0
    for start in range(0, len(keys), _ADD_CHUNK):
        chunk = keys[start : start + _ADD_CHUNK]
        try:
            async with client.pipeline(transaction=False) as pipe:
                for key in chunk:
                    for pos in _positions(key, m, k):
                        pipe.setbit(bitmap, pos, 1)
                await pipe.execute()
        except Exception:
            return written
        written += len(chunk)
    return written


async def might_contain(key: str) -> bool:
    """key 是否**可能**在集合中。

    - 返回 ``False`` ⇒ 一定不在（k 个位全为 0）；
    - 返回 ``True`` ⇒ 可能在，或**无法判定**（Redis 不可用/开关关闭 → fail-open 不拦）。

    本函数**不带门禁**，只回答位图本身；要据此做「拒绝」判定，必须走
    :func:`definitely_absent`（它会先确认位图已完整预热）。
    """
    if not _enabled():
        return True
    client = await redis_client.get_redis(_bitmap_key())
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


async def mark_seeded() -> bool:
    """打上「位图已完整预热」门禁标记（带 TTL）。成功才允许据此拒绝。

    标记过期（默认 7 天）即自动退回「不拦」——预热任务长期停摆时保护正确性，而非继续拿一份
    可能残缺的位图拒绝用户。fail-open：Redis 不可用 → ``False``。
    """
    if not _enabled():
        return False
    client = await redis_client.get_redis(_seeded_key())
    if client is None:
        return False
    try:
        await client.set(_seeded_key(), "1", ex=max(1, settings.bloom_filter_seed_ttl_s))
    except Exception:
        return False
    return True


async def unmark_seeded() -> bool:
    """清除门禁标记（运维/测试用）：清除后立即退回「不拦」。fail-open。"""
    client = await redis_client.get_redis(_seeded_key())
    if client is None:
        return False
    try:
        await client.delete(_seeded_key())
    except Exception:
        return False
    return True


async def definitely_absent_many(keys: Sequence[str]) -> set[str]:
    """批量子集判定：返回 ``keys`` 中**确定不可能存在**的那些（白名单外）。

    带门禁与 fail-open：开关关、Redis 不可用、命令异常、或**未预热** → 返回空集（一个都不拦）。
    一次 pipeline 收齐「门禁标记 + 每个 key 的 k 位」，故批量 N 个 key 只多一趟往返。
    """
    if not keys or not _enabled():
        return set()
    client = await redis_client.get_redis(_bitmap_key())
    if client is None:
        return set()
    m, k = _params(settings.bloom_filter_capacity, settings.bloom_filter_error_rate)
    bitmap = _bitmap_key()
    try:
        async with client.pipeline(transaction=False) as pipe:
            pipe.get(_seeded_key())
            for key in keys:
                for pos in _positions(key, m, k):
                    pipe.getbit(bitmap, pos)
            res = await pipe.execute()
    except Exception:
        return set()
    if not res[0]:
        return set()  # 未预热 → 不拦
    bits = res[1:]
    absent: set[str] = set()
    for i, key in enumerate(keys):
        # 任一位为 0 ⇒ 一定不在 ⇒ 确定不存在（白名单外）。
        if any(int(bit) == 0 for bit in bits[i * k : (i + 1) * k]):
            absent.add(key)
    return absent


async def definitely_absent(key: str) -> bool:
    """单个判定：``True`` ⇒ 该 key 确定不在白名单里（可安全短路由）。

    门禁与 fail-open 同 :func:`definitely_absent_many`。
    """
    return bool(await definitely_absent_many([key]))
