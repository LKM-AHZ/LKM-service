"""后台管理依赖端口：把「后台会话 + 2FA」的 FastAPI 依赖交给业务 router。

content/files/projects 里少数高危端点要以后台身份执行，原先直接
``from app.modules.admin.deps import require_admin_2fa``。依赖本身是 ``Depends(...)``
对象，必须在**路由模块导入期**就拿到，因此本模块的访问器是同步函数：

    Admin2FADep = Annotated[CurrentUser, admin_guard.require_admin_2fa()]

代价是端口必须在 ``app.main`` 导入前绑定——``boot.assemble()`` 正是先装配再导入，
漏掉装配会立刻 ``PortNotBound``，而不是让端点悄悄变成匿名可达。
"""

from __future__ import annotations

from typing import Any

from core.ports.registry import get


def require_admin_2fa() -> Any:
    """返回后台危险操作的依赖对象（FastAPI ``Depends`` 实例）。"""
    return get("admin_guard").require_admin_2fa
