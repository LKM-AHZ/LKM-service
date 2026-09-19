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
    """写入各复合角色默认权限；已存在则跳过（幂等）。返回实际新增行数。

    并发/重复执行安全：用 ``INSERT ... ON CONFLICT DO NOTHING`` 交由数据库按
    ``(role_name, permission)`` 唯一约束去重，避免 SELECT-再-INSERT 的竞态窗口
    （多 worker 首次建库同时 seed 时，不会因唯一约束冲突启动失败）。

    新增行数 = 插入后总量 - 插入前总量（result.rowcount 在 ty 的 SQLAlchemy
    stub 中缺失，遂改用计数差值，避免类型抑制注释）。
    """
    from sqlalchemy import func, select

    rows = _rows()
    if not rows:
        return 0
    before = int(
        (await db.scalar(select(func.count()).select_from(RolePermission))) or 0
    )
    # PostgreSQL ON CONFLICT：显式冲突目标（role+permission 唯一约束）防重复插入 → 幂等。
    from sqlalchemy.dialects.postgresql import insert as impl_insert

    stmt = impl_insert(RolePermission).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[RolePermission.role_name, RolePermission.permission]
    )
    await db.execute(stmt)
    await db.flush()
    after = int(
        (await db.scalar(select(func.count()).select_from(RolePermission))) or 0
    )
    return after - before


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
