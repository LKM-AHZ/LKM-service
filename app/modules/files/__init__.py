"""文件模块：上传/下载、内容寻址存储与事件通知。模块公共 API——跨模块 import 的唯一合法入口。"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None
_exported_graphql: list[Any] | None = None


def __getattr__(name: str) -> Any:
    global _exported_routers, _exported_graphql
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.files.internal_router import (
                router as files_internal_router,
            )
            from app.modules.files.media_router import router as files_media_router
            from app.modules.files.notify import router as files_notify_router
            from app.modules.files.router import router

            _exported_routers = [
                router,
                files_notify_router,
                files_internal_router,
                files_media_router,
            ]
        return _exported_routers
    if name == "GRAPHQL":
        if _exported_graphql is None:
            _exported_graphql = []
        return _exported_graphql
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
