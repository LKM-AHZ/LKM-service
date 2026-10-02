"""RBAC 权限判定：角色→权限点实时查表。

判定失败按拒绝处理（fail-closed）：查无映射返回 False，由调用方
（RequirePermission / require_permission）抛 FORBIDDEN。
"""

import uuid
from typing import Any

from app.modules.rbac.permissions import Permission
from app.modules.rbac.repository import ResourceRepository, RolePermissionRepository
from core.contracts import CurrentUser
from core.db.repository import DbSession
from core.err import BizError, CommonErr
from core.rbac_roles import composite_role, role_closure


async def role_has_permission(
    db: DbSession,
    role_name: str,
    permission: Permission,
) -> bool:
    """查询当前映射；撤销授权后下一次请求立即按数据库状态判定。

    授权结果不能使用通用 TTL 缓存，否则已撤销的权限仍可在缓存窗口内放行。
    """
    return await RolePermissionRepository(db).has_permission(
        role_name, permission.value
    )


async def user_has_permission(
    db: DbSession, cur: CurrentUser, permission: Permission
) -> bool:
    """会话已激活角色的权限并集；旧 CurrentUser 仍按基础角色判定。"""
    roles = cur.active_roles
    if roles is None:
        roles = (composite_role(cur.account_level, cur.role),)
    prefix = f"{cur.account_level}:"
    valid_roles = tuple(role for role in role_closure(roles) if role.startswith(prefix))
    if len(valid_roles) == 1:
        return await role_has_permission(db, valid_roles[0], permission)
    return await RolePermissionRepository(db).has_any_permission(
        valid_roles, permission.value
    )


async def set_role_permission(
    db: DbSession, role_name: str, permission: Permission, *, enabled: bool
) -> bool:
    """授予或显式撤销角色权限，由调用方在同一事务中提交及记录审计。

    禁用时保留记录，启动 seed 的 ``ON CONFLICT DO NOTHING`` 不会重新授权。
    返回是否发生状态变更，便于调用方只在变更时记审计事件。
    """
    return await RolePermissionRepository(db).set_permission(
        role_name, permission.value, enabled=enabled
    )


async def check_owner(
    db: DbSession,
    cur: CurrentUser,
    obj_id: uuid.UUID,
    model: type[Any],
    id_field: str,
    permission: Permission,
) -> None:
    """对象级权限断言：资源属主直接放行，否则检查代管权限点。

    先确认资源存在且未软删；普通属主无需再查角色权限映射。

    *permission* 是对象级权限点（如 ``content_owner_delete``）；非属主的管理员
    通过拥有该权限点获得代管资格（如 super_admin）。
    """
    owner_row = await ResourceRepository(db).get_owner_row(model, obj_id, id_field)
    if owner_row is None:
        raise BizError(CommonErr.FORBIDDEN)
    if owner_row[0] == cur.id:
        return

    if not await user_has_permission(db, cur, permission):
        raise BizError(CommonErr.FORBIDDEN)
