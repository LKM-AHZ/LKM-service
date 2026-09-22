"""RBAC 依赖工厂：路由层权限点校验（FastAPI 依赖注入）。

``RequirePermission(permission)`` 是全局权限点依赖工厂，供路由参数使用::

    @router.post("/content")
    async def create_content(
        cur: CurrentUser = RequirePermission(Permission.content_create),  # 工厂已返回 Depends，勿再包一层
        ...
    ): ...

对象级权限（属主/板块负责人/admin 代管）走 ``rbac.service.check_owner`` 谓词，
由各路由在 handler 内调用（FastAPI 无法动态透传路径参数名给依赖，故采用谓词方案）。
"""

from typing import Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.db.session import get_session
from app.modules.rbac.permissions import Permission, composible_role
from app.modules.rbac.service import role_has_permission
from auth.deps import CurrentUser, get_current_user


def RequirePermission(permission: Permission) -> Any:
    """全局权限点依赖工厂：当前用户有效角色须被授予指定权限点。"""

    async def checker(
        cur: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_session),
    ) -> CurrentUser:
        role = composible_role(cur.account_level, cur.role)
        if not await role_has_permission(db, role, permission):
            raise BizError(CommonErr.FORBIDDEN, f"Missing permission: {permission}")
        return cur

    return Depends(checker)
