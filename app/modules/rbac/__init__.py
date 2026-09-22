"""rbac 子包（权限框架的**实现**所在，非公开门面）。

公开面在子模块里，请直接从子模块导入：``rbac.permissions``（权限点/角色映射）、
``rbac.service``（check_owner 等谓词）、``rbac.deps``（RequirePermission 依赖工厂）。
本 ``__init__`` 刻意不 re-export 任何名字（保持 import 轻量、避免隐式循环），
故 ``from app.modules.rbac import Permission`` 会失败——请用
``from app.modules.rbac.permissions import Permission``。

本子包不挂载路由/GraphQL，故不暴露 ROUTERS/GRAPHQL。
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
