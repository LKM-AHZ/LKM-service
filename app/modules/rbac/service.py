"""RBAC 权限判定：角色→权限点查表（带短 TTL 缓存）。

判定失败按拒绝处理（fail-closed）：查无映射/缓存未命中均返回 False，由调用方
（RequirePermission / require_permission）抛 FORBIDDEN。
"""

import uuid
from typing import Any

from app.core.cache import cache_get, cache_set, make_key
from app.core.err import BizError, CommonErr
from app.db.repository import DbSession
from app.modules.auth.deps import CurrentUser
from app.modules.rbac.permissions import Permission, composible_role
from app.modules.rbac.repository import ResourceRepository, RolePermissionRepository

# 权限映射缓存 TTL（秒）：改动极低频，短 TTL 弱一致可接受（spec D7）
_PERM_TTL = 60


async def role_has_permission(
    db: DbSession,
    role_name: str,
    permission: Permission,
) -> bool:
    """查询复合角色是否被授予指定权限点。Redis 可用走短 TTL 缓存，否则直查库。"""
    key = make_key("rbac:perm", role_name, permission.value)
    cached = await cache_get(key)
    if cached is not None:
        return bool(cached)
    result = await RolePermissionRepository(db).has_permission(
        role_name, permission.value
    )
    await cache_set(key, result, _PERM_TTL)
    return result


async def check_owner(
    db: DbSession,
    cur: CurrentUser,
    obj_id: uuid.UUID,
    model: type[Any],
    id_field: str,
    permission: Permission,
) -> None:
    """对象级权限断言：拥有该 owner 权限点（admin 代管/板块负责人）即放行；
    否则查库判 ``resource.{id_field} == cur.id``。不满足抛 FORBIDDEN。

    *permission* 是对象级权限点（如 ``content_owner_delete``）；非属主的管理员
    通过拥有该权限点获得代管资格（如 super_admin）。
    """
    role = composible_role(cur.account_level, cur.role)
    if await role_has_permission(db, role, permission):
        return

    obj = await ResourceRepository(db).get_by_model(model, obj_id)
    if obj is None:
        # CommonErr 无 NOT_FOUND（仅 INVALID_INPUT/FORBIDDEN/INTERNAL_ERROR/MFA_REQUIRED）。
        # 对象不存在时不返回 404（避免泄露资源存在性），统一 FORBIDDEN。
        raise BizError(CommonErr.FORBIDDEN)
    if getattr(obj, id_field) != cur.id:
        raise BizError(CommonErr.FORBIDDEN)
