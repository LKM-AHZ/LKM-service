"""rbac 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

- 角色权限点查表（``role_permissions``，模型归 admin 域，此处只读判定）。
- 对象级属主通用读取：``check_owner`` 的 ``model``/``id_field`` 由调用方运行期给出，
  故 :class:`ResourceRepository` 不绑定固定 model，只借基类的会话属性。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.repository import AsyncRepository
from app.modules.admin.models import RolePermission


class RolePermissionRepository(AsyncRepository[RolePermission]):
    model = RolePermission

    async def has_permission(self, role_name: str, permission: str) -> bool:
        """该角色是否被授予指定权限点。"""
        return await self.exists(
            RolePermission.role_name == role_name,
            RolePermission.permission == permission,
        )


class ResourceRepository(AsyncRepository[Any]):
    """对象级属主查询的资源缝。

    不绑定具体 ``model``（由 ``check_owner`` 运行期传入），故不使用依赖
    ``self.model`` 的基类通用方法，只借 ``self.db`` 会话。
    """

    model = object  # type: ignore[assignment]

    async def get_by_model(self, model: type[Any], obj_id: uuid.UUID) -> Any | None:
        return await self.db.get(model, obj_id)
