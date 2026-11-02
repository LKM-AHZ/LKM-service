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

from fastapi import Depends

from core.ports.registry import PortNotBound, get, is_bound


async def _unbound() -> Any:
    """未装配时的占位依赖：**请求期**才失败，绝不静默放行。"""
    raise PortNotBound("端口 'admin_guard' 未绑定：后台端点须先经 boot.assemble() 装配")


def require_admin_2fa() -> Any:
    """返回后台危险操作的依赖对象（FastAPI ``Depends`` 实例）。

    装配完成后返回 admin 域的真实依赖；**未装配时**返回一个请求期才失败的占位依赖。
    这样 ``import app.main`` 本身不会因端口未绑定而崩溃——装配时序由
    ``tests/test_boot_assembly_order.py`` 单独守护，本端口不该改变那条既有不变量。
    占位依赖一旦被真正调用就 500（fail-closed），不会退化成匿名可达。
    """
    if is_bound("admin_guard"):
        return get("admin_guard").require_admin_2fa
    return Depends(_unbound)
