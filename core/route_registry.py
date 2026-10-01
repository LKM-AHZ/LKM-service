"""
跨包路由注册表：app 侧装配 API 时不直接 import auth 的路由。
拆分前 ``app/api/router.py`` 直接 ``from auth import ROUTERS``。现在由 auth 在自己的
bootstrap 里 :func:`register_routers` 登记，app 侧只向 core 取——两个方向都不产生 import。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_extra_routers: list[Any] = []


def register_routers(routers: Iterable[Any]) -> None:
    """登记一组路由（幂等：同一对象重复登记只保留一份）。"""
    for r in routers:
        if r not in _extra_routers:
            _extra_routers.append(r)


def extra_routers() -> list[Any]:
    """取全部已登记路由（返回副本，避免调用方就地改动注册表）。"""
    return list(_extra_routers)


def reset() -> None:
    """清空登记（仅供测试隔离）。"""
    _extra_routers.clear()
