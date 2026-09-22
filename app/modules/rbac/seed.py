"""幂等 seed：按 DEFAULT_GRANTS 写入复合角色→权限点默认映射。

用法::

    python -m app.modules.rbac.seed

也可经 ``init_db`` 在应用启动时自动调用（见 app/db/init_db.py）。
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base  # noqa: F401  （随 auth.models 一起确保模型元数据可见）
from app.db.session import new_session
from app.modules.admin.models import (
    RolePermission,
)
from app.modules.rbac.permissions import DEFAULT_GRANTS
from auth import register_models

register_models()  # 注册 auth ORM 映射类（幂等）


def _rows() -> list[dict[str, str]]:
    return [
        {"role_name": role_name, "permission": grant.permission.value}
        for role_name, grants in DEFAULT_GRANTS.items()
        for grant in grants
    ]


async def seed_rbac(db: AsyncSession) -> int:
    """对账写入各复合角色默认权限；返回实际新增行数。

    并发/重复执行安全：用 ``INSERT ... ON CONFLICT DO NOTHING`` 交由数据库按
    ``(role_name, permission)`` 唯一约束去重，避免 SELECT-再-INSERT 的竞态窗口
    （多 worker 首次建库同时 seed 时，不会因唯一约束冲突启动失败）。

    新增行数取语句自身的 rowcount：原先用插入前后整表 COUNT 差值，会把并发 worker
    同时插入/删除的行算进来，日志里的「实际新增」可以偏大、偏小甚至为负。
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy.dialects.postgresql import insert as impl_insert

    rows = _rows()
    if not rows:
        return 0

    # 先对账删除：DEFAULT_GRANTS 仍管理的角色下、已从代码里移除的授权行必须清掉，
    # 否则 role_permissions（运行时真相源）会保留代码已删除的权限，两处长期漂移
    for role_name, grants in DEFAULT_GRANTS.items():
        allowed = [g.permission.value for g in grants]
        await db.execute(
            sa_delete(RolePermission).where(
                RolePermission.role_name == role_name,
                RolePermission.permission.notin_(allowed),
            )
        )

    # PostgreSQL ON CONFLICT：显式冲突目标（role+permission 唯一约束）防重复插入 → 幂等。
    stmt = impl_insert(RolePermission).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[RolePermission.role_name, RolePermission.permission]
    )
    result = await db.execute(stmt)
    await db.flush()
    # rowcount 在 ty 的 SQLAlchemy stub 里缺失，沿用仓库既有 getattr 兜底写法
    return int(getattr(result, "rowcount", 0) or 0)


async def _main() -> None:
    db = (
        await new_session()
    )  # new_session() 返回单个 AsyncSession（非 factory），与 boards/seed.py 一致
    try:
        n = await seed_rbac(db)
        await db.commit()
        print(f"seed_rbac: inserted {n} role-permission rows")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
