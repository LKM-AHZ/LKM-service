"""rbac 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

- 角色权限点查表（``role_permissions``，模型归 admin 域，此处只读判定）。
- 对象级属主通用读取：``check_owner`` 的 ``model``/``id_field`` 由调用方运行期给出，
  故 :class:`ResourceRepository` 不绑定固定 model，也不继承模型化 CRUD 基类。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.repository import AsyncRepository, DbSession
from app.modules.admin.models import RolePermission


class RolePermissionRepository(AsyncRepository[RolePermission]):
    model = RolePermission

    async def has_permission(self, role_name: str, permission: str) -> bool:
        """该角色是否被授予指定权限点。"""
        return await self.exists(
            RolePermission.role_name == role_name,
            RolePermission.permission == permission,
        )


class ResourceRepository:
    """对象级属主查询的资源缝。

    不绑定具体 ``model``（由 ``check_owner`` 运行期传入），故**不继承**模型化 CRUD 基类：
    原先以 ``model = object`` 占位继承 ``AsyncRepository``，会让那批依赖 ``self.model`` 的
    方法（get/get_many/count/exists/create/update_where/pg_upsert/soft_delete_where…）
    静默对着 object 发查询、在 SQLAlchemy 深处以难懂的方式炸开，而不是在契约层面直接失败。
    本类只保留会话与真正被调用的一个方法。
    """

    def __init__(self, db: DbSession) -> None:
        self.db = db

    async def get_by_model(self, model: type[Any], obj_id: uuid.UUID) -> Any | None:
        return await self.db.get(model, obj_id)
