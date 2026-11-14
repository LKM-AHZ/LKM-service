"""进程级生命周期钩子注册表。

core 不知道业务插件存在：``boot`` 装配时把「遍历插件并调用其钩子」的函数注册进来，
各进程入口（backend lifespan、worker 消费循环）只调 :func:`run_startup` /
:func:`run_shutdown`。未装配的进程（如 ``auth.main``）调用是 no-op。

幂等：``run_startup`` 在同一进程只执行一次——default worker 并发消费多个订阅，
每个消费循环都会进入 ``run_startup``，不能因此把插件初始化跑多遍。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("lkm.lifecycle")

_Hook = Callable[[], Awaitable[None]]

_startup_hooks: list[_Hook] = []
_shutdown_hooks: list[_Hook] = []
_started = False


def register_startup(hook: _Hook) -> None:
    """登记启动钩子（装配期调用，重复登记同一函数幂等）。"""
    if hook not in _startup_hooks:
        _startup_hooks.append(hook)


def register_shutdown(hook: _Hook) -> None:
    """登记关闭钩子；执行顺序与启动相反（LIFO）。"""
    if hook not in _shutdown_hooks:
        _shutdown_hooks.append(hook)


async def run_startup() -> None:
    """按登记顺序执行启动钩子；失败即抛出（进程启动失败优于半初始化运行）。"""
    global _started
    if _started:
        return
    for hook in tuple(_startup_hooks):
        await hook()
    _started = True


async def run_shutdown() -> None:
    """按 LIFO 执行关闭钩子；单个钩子失败只记日志，不阻断其余清理。"""
    global _started
    if not _started:
        return
    _started = False
    for hook in reversed(tuple(_shutdown_hooks)):
        try:
            await hook()
        except Exception:
            logger.exception("lifecycle shutdown hook failed")


def reset() -> None:
    """清空钩子与状态（仅供测试隔离）。"""
    global _started
    _startup_hooks.clear()
    _shutdown_hooks.clear()
    _started = False
