"""日历域：用户日程事件 + 日历订阅。模块公共 API——跨模块 import 的唯一合法入口。"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.calendar.router import router

            _exported_routers = [router]
        return _exported_routers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
