"""幂等 seed：按 DEFAULT_GRANTS 写入复合角色→权限点默认映射。

用法::

    python -m app.modules.rbac.seed

也可经 ``init_db`` 在应用启动时自动调用（见 app/db/init_db.py）。
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import (
    RolePermission,
)
from app.modules.rbac.permissions import DEFAULT_GRANTS
from core.db.base import Base  # noqa: F401  （随 auth.models 一起确保模型元数据可见）
from core.db.session import new_worker_session as new_session


def _rows() -> list[dict[str, str]]:
    return [
        {"role_name": role_name, "permission": grant.permission.value}
        for role_name, grants in DEFAULT_GRANTS.items()
        for grant in grants
    ]


async def seed_rbac(db: AsyncSession) -> int:
    """补齐各复合角色默认权限；保留额外授权和显式禁用的默认授权。

    并发/重复执行安全：用 ``INSERT ... ON CONFLICT DO NOTHING`` 交由数据库按
    ``(role_name, permission)`` 唯一约束去重，避免 SELECT-再-INSERT 的竞态窗口
    （多 worker 首次建库同时 seed 时，不会因唯一约束冲突启动失败）。

    新增行数取语句自身的 rowcount：原先用插入前后整表 COUNT 差值，会把并发 worker
    同时插入/删除的行算进来，日志里的「实际新增」可以偏大、偏小甚至为负。
    """
    from sqlalchemy.dialects.postgresql import insert as impl_insert

    rows = _rows()
    if not rows:
        return 0

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
