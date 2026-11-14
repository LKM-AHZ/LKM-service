"""插件生命周期钩子：把 ``app.modules.spec.SPECS`` 上的可选钩子接到 core.lifecycle。

约定（全部可选，同步/异步均可）：

- ``plugin_startup()``：进程开始服务前调用（按清单依赖顺序）；
- ``plugin_shutdown()``：进程退出/取消时调用（逆序）。

启动失败直接抛错（半初始化运行比启动失败更危险）；关闭失败只记日志，不阻断其余插件
与进程清理。钩子只在调用 :func:`boot.assemble.assemble` 的进程里注册；``auth.main``
不装配业务插件，``run_startup`` 是 no-op。
"""

from __future__ import annotations

import inspect
import logging
from importlib import import_module

logger = logging.getLogger("lkm.plugins")

_STARTUP_HOOK = "plugin_startup"
_SHUTDOWN_HOOK = "plugin_shutdown"


async def _call(module_path: str, hook_name: str) -> None:
    module = import_module(module_path)
    hook = getattr(module, hook_name, None)
    if hook is None:
        return
    result = hook()
    if inspect.isawaitable(result):
        await result


async def _startup_all() -> None:
    from app.modules.spec import SPECS

    for spec in SPECS:
        try:
            await _call(spec.module_path, _STARTUP_HOOK)
        except Exception as exc:
            raise RuntimeError(
                f"插件 {spec.name!r} 的 {_STARTUP_HOOK} 失败：{exc}"
            ) from exc


async def _shutdown_all() -> None:
    from app.modules.spec import SPECS

    for spec in reversed(SPECS):
        try:
            await _call(spec.module_path, _SHUTDOWN_HOOK)
        except Exception:
            logger.exception("插件 %r 的 %s 失败", spec.name, _SHUTDOWN_HOOK)


def register_hooks() -> None:
    """装配期调用：把插件钩子接入 ``core.lifecycle``（幂等）。"""
    from core import lifecycle

    lifecycle.register_startup(_startup_all)
    lifecycle.register_shutdown(_shutdown_all)
