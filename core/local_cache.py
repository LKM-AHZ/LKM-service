"""
L1 本地进程内缓存：有界 TTL + LRU，仅作 L2/权威的只读加速镜像。
- **不具权威**：L1 只是 L2 的镜像；防 LWW/CAS/对账收敛仍以 L2/DB 为准（见 user_cache）。
- **有界**：超过 ``settings.user_snap_l1_maxsize`` 按 LRU 逐出，防 user 数增长内存无界。
- **惰性过期**：get 时比对 ``time.monotonic()`` 截止时间，不注册定时器。
- **批量读取**：与 aiocache 的 ``multi_get`` 一样保持输入顺序，并共用一次时钟采样。
- **回填防竞态**：跨 ``await`` 的 L2 读取只在本地未发生失效、目标键仍为空时回填。
- 同一事件循环内操作无 await，检查和写入之间不会切换协程。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from core.config import settings

# key → (deadline_monotonic, value)
_data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_maxsize: int = max(1, settings.user_snap_l1_maxsize)
_invalidation_revision = 0


def _now() -> float:
    return time.monotonic()


def _get(key: str, now: float) -> Any | None:
    item = _data.get(key)
    if item is None:
        return None
    deadline, value = item
    if deadline <= now:
        _data.pop(key, None)
        return None
    _data.move_to_end(key)
    return value


def l1_get(key: str) -> Any | None:
    """命中且未过期返回值（并刷新 LRU 位置）；未命中/过期 → None。"""
    return _get(key, _now())


def l1_multi_get(keys: list[str]) -> list[Any | None]:
    """按输入顺序读取多个键；所有键按同一个 monotonic 时刻判断过期。"""
    if not keys:
        return []
    now = _now()
    return [_get(key, now) for key in keys]


def l1_invalidation_revision() -> int:
    """读取本进程失效代次，供跨 await 的 L2 回填防竞态。"""
    return _invalidation_revision


def l1_set_if_unchanged(key: str, value: Any, ttl: float, revision: int) -> bool:
    """仅在读取 L2 期间没有失效、且目标键未被其他协程回填时写入。"""
    if (
        ttl <= 0
        or value is None
        or revision != _invalidation_revision
        or _get(key, _now()) is not None
    ):
        return False
    l1_set(key, value, ttl)
    return True


def l1_set(key: str, value: Any, ttl: float) -> None:
    """
    写入并设置 TTL；超容量按 LRU 逐出最旧条目。ttl<=0 视为不缓存。
    """
    if ttl <= 0 or value is None:
        # ttl<=0 表示「本次不要缓存这条」：必须把旧条目一并清掉
        l1_delete(key)
        return
    _data[key] = (_now() + ttl, value)
    _data.move_to_end(key)
    while len(_data) > _maxsize:
        _data.popitem(last=False)


def l1_delete(key: str) -> None:
    """删除单键（失效广播/本地失效调用口）。"""
    global _invalidation_revision
    _invalidation_revision += 1
    _data.pop(key, None)


def l1_clear() -> None:
    """清空全部条目。"""
    global _invalidation_revision
    _invalidation_revision += 1
    _data.clear()


def l1_size() -> int:
    """当前条目数（测试/观测用）。"""
    return len(_data)


def reset(maxsize: int | None = None) -> None:
    """清空并重读配置（测试隔离/settings 变更后用）。"""
    global _invalidation_revision, _maxsize
    _invalidation_revision += 1
    _data.clear()
    _maxsize = max(1, maxsize if maxsize is not None else settings.user_snap_l1_maxsize)
