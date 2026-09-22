"""互动计数 Redis 增量原语（M6.10）。

设计口径（详见执行路线图 §8 登记）:

- **单向增量**：写路径只向 Redis ``INCRBY`` 一个差值（``+1``/``-1``），不读改写、
  不做「DB 计数 ±1」的热点行争用。DB 计数列由 ``flush`` 周期落库。
- **真相源是明细表**：Redis 只是「尚未落库的差值」缓冲，不是计数权威；权威由
  ``reconcile`` 从明细行 ``COUNT(*)`` 重算（可证伪：二次对账 diff=0）。
- **fail-open**：Redis 未启用/不可达时 ``bump_counter`` 返回 ``False``，调用方回退
  到原有的「原子 UPDATE DB」路径，行为与引入本链路前一致。

本模块只依赖 Redis，不 import 任何业务模型（sqlalchemy 列映射在
``app.modules.content.counters``）；键规范与 ``core.cache`` 一致（``lkm:{env}:...``）。

键：``lkm:{env}:count:{field}|{obj_id}``，值 = 待落库差值（可为负，表示净减）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core import redis as redis_client
from app.core.cache import make_key

logger = logging.getLogger(__name__)

# 允许计数化的列名白名单（防止把任意列名拼进键/UPDATE）
COUNTER_FIELDS: frozenset[str] = frozenset(
    {"like_count", "comment_count", "bookmark_count"}
)

# 键形如 lkm:{env}:count:{field}|{obj_id} → 冒号分段共 4 段
_KEY_SEGMENTS = 4


def counter_key(field: str, obj_id: uuid.UUID) -> str:
    if field not in COUNTER_FIELDS:
        raise ValueError(f"unsupported counter field: {field!r}")
    return make_key("count", field, obj_id)


def parse_counter_key(key: str) -> tuple[str, uuid.UUID] | None:
    """从 Redis 键解析 ``(field, obj_id)``；非计数键返回 ``None``。

    ``obj_id`` 统一返回 ``uuid.UUID``（与写侧 :func:`counter_key` 收到的类型一致，也与
    ``content_items.id`` 等主键类型一致）；Redis 键里是 ``str(uuid)``，此处解析回对象。
    """
    parts = key.split(":")
    if len(parts) != _KEY_SEGMENTS:
        return None
    if parts[2] != "count":
        return None
    payload = parts[-1]
    field, _, raw_id = payload.partition("|")
    if field not in COUNTER_FIELDS:
        return None
    try:
        obj_id = uuid.UUID(raw_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return field, obj_id


async def bump_counter(field: str, obj_id: uuid.UUID, delta: int) -> bool:
    """把差值记入 Redis。返回 ``True`` 表示已入 Redis（DB 待 flush 收敛）。"""
    # 键构造（含白名单校验）必须在 try 之外：字段名非法是调用方错误，不能与
    # 「Redis 不可用」共用同一条 False 出口，否则会被静默导到 DB 回退路径
    key = counter_key(field, obj_id)
    client = await redis_client.get_redis()
    if client is None:
        return False
    try:
        await client.incrby(key, delta)
    except Exception:
        logger.warning("count incrby 失败，回退 DB 路径 key=%s", key, exc_info=True)
        return False
    return True


async def pending_delta(field: str, obj_id: uuid.UUID) -> int:
    """当前未落库差值（Redis 不可用/无键 → 0）。用于返回「DB 值 + 增量」的即时读数。"""
    key = counter_key(field, obj_id)  # 同 bump_counter：校验失败必须外抛，不吞成 0
    client = await redis_client.get_redis()
    if client is None:
        return 0
    try:
        raw = await client.get(key)
    except Exception:
        logger.warning("count 读增量失败，按 0 处理 key=%s", key, exc_info=True)
        return 0
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


async def drain_counters() -> dict[tuple[str, uuid.UUID], int]:
    """原子取走全部待落库差值（``GETDEL``），返回 ``{(field, obj_id): delta}``。

    用 ``SCAN`` 而非维护 pending 集合：无「集合成员与实际键不同步」的竞态窗口，
    并发新增的键会被下一轮 SCAN 扫到（最坏是晚一轮落库，且对账兜底）。
    取走即清零——若随后 DB 写入失败，差值丢失（宁少不重），由对账收敛回真值。
    """
    client = await redis_client.get_redis()
    if client is None:
        return {}

    drained: dict[tuple[str, uuid.UUID], int] = {}
    pattern = make_key("count", "*")
    try:
        async for key in client.scan_iter(match=pattern, count=500):
            parsed = parse_counter_key(key)
            if parsed is None:
                continue
            raw: Any = await client.getdel(key)
            if raw is None:
                continue
            try:
                delta = int(raw)
            except (TypeError, ValueError):
                continue
            if delta:
                drained[parsed] = drained.get(parsed, 0) + delta
    except Exception:
        # 扫描中途失败：已取走的差值仍会被调用方落库（返回已收集部分），
        # 但必须留痕——例如 Redis < 6.2 没有 GETDEL，会每轮都在这里静默中断
        logger.warning(
            "drain_counters 扫描中断，已取走 %d 项仍将落库", len(drained), exc_info=True
        )
        return drained
    return drained
