"""后台登录态只读端点：monolith 保留 /admin/auth/me（S5-A2 Step1）。

4 个会话**写面**（login/refresh/logout/2fa）已迁 AUTH 进程
（``auth.admin_router``，DB 走独立 auth 库，在 auth_http seam 开时 monolith
不再 serve 该 URL → 自然 404）。本文件现仅余 /me：读取态，用 require_admin(seam-only)
+ require_permission(业务 db 判定复合角色持仓) 返回 id/account_level/role。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import respond
from app.db.session import get_session
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser

from .deps import require_admin
from .permissions import require_permission

router = APIRouter(prefix="/admin", tags=["admin-auth"])


@router.get("/auth/me")
@respond
async def admin_me(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> dict[str, int | str]:
    """当前后台登录态（需有效 admin 会话，经 seam-only require_admin + 复合角色持仓权限点）。

    供前端 bootAdminSession 使用；role 判定依赖业务 db 的 RolePermission(super_admin 默认
    grants)，故走 get_session 注入同一会话后叠加 admin_dashboard 权限点。
    """
    await require_permission(db, cur, Permission.admin_dashboard)
    return {
        "id": cur.id,
        "account_level": cur.account_level,
        "role": cur.role,
    }
