"""backend（ASGI）进程入口：装配后暴露业务 FastAPI app（不含 auth 路由）。

``uvicorn main:app`` → 仓库根 ``main.py`` → 本模块。``assemble()`` 负责模型/任务/端口绑定，
保持在 ``import app.main`` 之前，避免路由构建期间读到未绑定的端口。
"""

from __future__ import annotations

from boot.assemble import assemble

assemble()

from app.main import app  # noqa: E402  # 必须在 assemble() 之后

__all__ = ["app"]
