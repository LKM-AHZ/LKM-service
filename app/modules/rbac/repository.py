"""rbac 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

- 角色权限点查表（``role_permissions``，模型归 admin 域，此处只读判定）。
- 对象级属主列读取：``check_owner`` 的 ``model``/``id_field`` 由调用方运行期给出，
  故 :class:`ResourceRepository` 不绑定固定 model，也不继承模型化 CRUD 基类。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.modules.admin.models import RolePermission
from core.db.repository import AsyncRepository, DbSession


class RolePermissionRepository(AsyncRepository[RolePermission]):
    model = RolePermission

    async def has_permission(self, role_name: str, permission: str) -> bool:
        """该角色是否有当前启用的授权。"""
        return await self.exists(
            RolePermission.role_name == role_name,
            RolePermission.permission == permission,
            RolePermission.enabled.is_(True),
        )

    async def has_any_permission(
        self, role_names: tuple[str, ...], permission: str
    ) -> bool:
        """一次查询判定激活角色的权限并集。"""
        if not role_names:
            return False
        return await self.exists(
            RolePermission.role_name.in_(role_names),
            RolePermission.permission == permission,
            RolePermission.enabled.is_(True),
        )

    async def set_permission(
        self, role_name: str, permission: str, *, enabled: bool
    ) -> bool:
        """原子设置授权状态；返回数据库状态是否实际变化。"""
        stmt = pg_insert(RolePermission).values(
            role_name=role_name, permission=permission, enabled=enabled
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RolePermission.role_name, RolePermission.permission],
            set_={"enabled": enabled},
            where=RolePermission.enabled.is_distinct_from(enabled),
        )
        result = await self.db.execute(stmt)
        return bool(getattr(result, "rowcount", 0))


class ResourceRepository:
    """对象级属主查询的资源缝。

    不绑定具体 ``model``（由 ``check_owner`` 运行期传入），故**不继承**模型化 CRUD 基类：
    原先以 ``model = object`` 占位继承 ``AsyncRepository``，会让那批依赖 ``self.model`` 的
    方法（get/get_many/count/exists/create/update_where/pg_upsert/soft_delete_where…）
    静默对着 object 发查询、在 SQLAlchemy 深处以难懂的方式炸开，而不是在契约层面直接失败。
    本类只保留会话与真正被调用的属主列查询。
    """

    def __init__(self, db: DbSession) -> None:
        self.db = db

    async def get_owner_row(
        self, model: type[Any], obj_id: uuid.UUID, id_field: str
    ) -> tuple[Any] | None:
        """只读取属主列；无资源返回 None，并过滤软删。

        不能用 ``self.db.get()``：它绕过 AsyncRepository._active_conditions 的软删条件，
        已删除的资源仍会解析成功并通过 check_owner 的属主校验。
        """
        stmt = select(getattr(model, id_field)).where(model.id == obj_id)
        if hasattr(model, "deleted_at"):
            stmt = stmt.where(model.deleted_at.is_(None))
        row = (await self.db.execute(stmt)).first()
        return (row[0],) if row is not None else None
