"""L1 本地进程内缓存（roadmap §5.6）：有界 TTL + LRU，仅作 L2/权威的只读加速镜像。

为什么自实现而非 aiocache：aiocache 当前稳定版(0.12.x) 的 ``SimpleMemoryCache`` **无
``maxsize``**（1.0.0a0 才有），且带 TTL 的 set 注册 loop 绑定的 ``call_later``，跨测试事件
循环会残留条目。此处约 40 行的有界 TTL+LRU 字典语义等价、无 loop 绑定、可精确 reset，
满足「L1 有界、短 TTL、测试可隔离」的硬要求。

- **不具权威**：L1 只是 L2 的镜像；防 LWW/CAS/对账收敛仍以 L2/DB 为准（见 user_cache）。
- **有界**：超过 ``settings.user_snap_l1_maxsize`` 按 LRU 逐出，防 user 数增长内存无界。
- **惰性过期**：get 时比对 ``time.monotonic()`` 截止时间，不注册定时器。
- 进程内单协程访问，dict 操作无 await，天然原子。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from app.core.config import settings

# key → (deadline_monotonic, value)
_data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_maxsize: int = max(1, settings.user_snap_l1_maxsize)


def _now() -> float:
    return time.monotonic()


def l1_get(key: str) -> Any | None:
    """命中且未过期返回值（并刷新 LRU 位置）；未命中/过期/空值 → None。

    注意 None 同时是「未命中」哨兵：本缓存不接受 None 作为可缓存值
    （``l1_set`` 会忽略它），否则「命中且值为 None」与 miss 无法区分。
    """
    item = _data.get(key)
    if item is None:
        return None
    deadline, value = item
    if deadline <= _now():
        _data.pop(key, None)
        return None
    _data.move_to_end(key)
    return value


def l1_set(key: str, value: Any, ttl: float) -> None:
    """写入并设置 TTL；超容量按 LRU 逐出最旧条目。ttl<=0 视为不缓存。

    ``value is None`` 直接忽略：None 已是 miss 哨兵（见 ``l1_get``），存进去永远
    命不中，只会让人误以为「这条缓存过了」。
    """
    if ttl <= 0 or value is None:
        # ttl<=0 表示「本次不要缓存这条」：必须把旧条目一并清掉，
        # 否则调用方以为已放弃、读者却仍会命中它的旧值（陈旧数据）
        _data.pop(key, None)
        return
    _data[key] = (_now() + ttl, value)
    _data.move_to_end(key)
    while len(_data) > _maxsize:
        _data.popitem(last=False)


def l1_delete(key: str) -> None:
    """删除单键（失效广播/本地失效调用口）。"""
    _data.pop(key, None)


def l1_clear() -> None:
    """清空全部条目。"""
    _data.clear()


def l1_size() -> int:
    """当前条目数（测试/观测用）。"""
    return len(_data)


def reset(maxsize: int | None = None) -> None:
    """清空并重读配置（测试隔离/settings 变更后用）。"""
    global _maxsize
    _data.clear()
    _maxsize = max(1, maxsize if maxsize is not None else settings.user_snap_l1_maxsize)
