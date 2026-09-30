"""backend（ASGI）进程入口：装配后暴露含 auth 前台路由的 FastAPI app。

``uvicorn main:app`` → 仓库根 ``main.py`` → 本模块。装配必须早于 ``import app.main``：
``app/api/router.py`` 在模块级从 ``core.route_registry`` 取跨包路由，而登记发生在
``auth.bootstrap.register()``。
"""

from __future__ import annotations

from boot.assemble import assemble

assemble()

from app.main import app  # noqa: E402  # 必须在 assemble() 之后

__all__ = ["app"]
