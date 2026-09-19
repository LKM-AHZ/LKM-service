"""后台细粒度权限点叠加：在 `require_admin` 门槛之上，校验复合角色持有指定权限点。

后台鉴权是独立 cookie 会话（`require_admin`，判断 account_level==admin），本身不细粒度，
任何 org_member/super_admin 都能进后台。这里再叠加 RBAC 权限点判定：用当前 admin 的
``account_level`` 与 ``profile.role`` 拼出复合角色 ``{account_level}:{role}``，查
``role_permissions`` 表确认是否持有指定权限点；不持有抛 ``CommonErr.FORBIDDEN``。

运行期需透传与端点同一的 db 会话（users/reports 用 get_read_session 注入，content 用
get_session 注入；两者在测试与生产均指向同一库，判定一致）。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.modules.rbac.permissions import Permission, composible_role
from app.modules.rbac.service import role_has_permission
from auth.deps import CurrentUser


async def require_permission(
    db: AsyncSession,
    admin: CurrentUser,
    permission: Permission,
) -> None:
    """断言当前后台 admin 的复合角色持有指定权限点，否则抛 FORBIDDEN。

    *db* 复用端点注入的会话（get_read_session / get_session）。
    *admin* 是 `require_admin` 依赖解析出的 CurrentUser（已强制 account_level=admin）。
    *permission* 为目标域权限点（如 ``admin.users_manage``）。
    """
    role = composible_role(admin.account_level, admin.role)
    if not await role_has_permission(db, role, permission):
        raise BizError(CommonErr.FORBIDDEN, f"Missing permission: {permission.value}")
