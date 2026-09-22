"""按 key 的进程内请求合并（singleflight，roadmap §5.4/§5.6 防击穿）。

同一进程内并发请求同一 key 时，只有一个协程真正执行 loader，其余协程复用其返回值——
热点 key 失效瞬间不会同时打穿 AUTH/DB。

本模块按**引用计数**回收 flight，key 归零（含无界 key，如 user_id 数量级）即移出字典，
不会随 key 增长泄漏内存；``core.cache.cached_read`` 的并发单飞也复用本模块（原先自持
常驻锁字典，键含用户可控 slug 与每次 bump 都变的版本号，字典只增不减）。

- owner 以 ``asyncio.create_task(loader())`` 执行；所有调用方 ``await asyncio.shield(task)``，
  owner 请求被取消不会连带取消共享加载（其余等待方仍拿到结果）。
- loader 异常经 shield 传播给所有等待方；``done_callback`` 消费异常，避免 task 结束后
  "exception was never retrieved" 告警。
- 不再叠加跨进程 L2 互斥锁（本期从简；见路线图 §8 登记）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any


@dataclass
class _Flight:
    refs: int
    task: asyncio.Task[Any]


_flights: dict[str, _Flight] = {}
_guard = asyncio.Lock()


def _on_task_done(key: str, flight: _Flight, task: asyncio.Task[Any]) -> None:
    """消费 task 结果，防未取异常告警（结果仍由 await shield 的调用方取得）。"""
    if not task.cancelled():
        with suppress(Exception):
            task.exception()
    # 最后一个等待方在任务完成前被取消时，run() 的 finally 不敢回收表项（shield 仍在跑
    # loader），须由这里兜底回收；否则该 key 会永久驻留，后续请求会复用已完成任务的
    # 陈旧结果。refs<=0 保证无人在等，回调与 run() 同处事件循环线程，判断是原子的。
    if flight.refs <= 0 and _flights.get(key) is flight:
        _flights.pop(key, None)


async def run[T](
    key: str,
    loader: Callable[[], Awaitable[T]],
    *,
    on_role: Callable[[str], None] | None = None,
) -> T:
    """合并同 key 并发调用；``on_role`` 可选回调（"leader"/"shared"，观测用）。"""
    async with _guard:
        flight = _flights.get(key)
        is_leader = flight is None
        if flight is None:
            task: asyncio.Task[Any] = asyncio.create_task(loader())
            flight = _Flight(refs=0, task=task)
            task.add_done_callback(partial(_on_task_done, key, flight))
            _flights[key] = flight
        flight.refs += 1
    try:
        # 观测回调放进 try 内：它一旦抛出（如指标钩子出问题），refs 已自增却走不到下面的
        # finally，引用计数永久泄漏 → 该 key 的 flight 再也不会被回收（leader 路径还会
        # 让后续请求一直复用那个已完成任务的陈旧结果）
        if on_role is not None:
            on_role("leader" if is_leader else "shared")
        return await asyncio.shield(flight.task)
    finally:
        async with _guard:
            flight.refs -= 1
            # 任务未完成时不回收：否则最后一个等待方被取消会留下在途 loader 却清掉表项，
            # 后续同 key 请求再起一个并发 loader（去重失效）；留给 done 回调回收。
            if (
                flight.refs <= 0
                and flight.task.done()
                and _flights.get(key) is flight
            ):
                _flights.pop(key, None)


def in_flight() -> int:
    """当前在途 flight 数（测试断言引用计数回收用）。"""
    return len(_flights)


def reset() -> None:
    """清空 flight 表（仅测试用；生产无调用）。"""
    _flights.clear()
