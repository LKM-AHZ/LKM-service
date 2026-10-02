"""seed_rbac 幂等写入默认映射。"""

from sqlalchemy import func, select

from app.modules.admin.models import RolePermission
from app.modules.rbac.permissions import DEFAULT_GRANTS, Permission
from app.modules.rbac.seed import seed_rbac
from app.modules.rbac.service import role_has_permission, set_role_permission
from tests.conftest import DB


async def test_seed_writes_default_grants(db: DB) -> None:
    inserted = await seed_rbac(db)
    assert inserted == sum(len(v) for v in DEFAULT_GRANTS.values())
    total = (
        await db.execute(select(func.count()).select_from(RolePermission))
    ).scalar_one()
    assert total == inserted


async def test_seed_idempotent(db: DB) -> None:
    await seed_rbac(db)
    first = (
        await db.execute(select(func.count()).select_from(RolePermission))
    ).scalar_one()
    await seed_rbac(db)  # 再次执行不重复
    second = (
        await db.execute(select(func.count()).select_from(RolePermission))
    ).scalar_one()
    assert first == second


async def test_seed_preserves_custom_grant(db: DB) -> None:
    custom = RolePermission(
        role_name="normal:member", permission=Permission.articles_review.value
    )
    db.add(custom)
    await db.flush()

    await seed_rbac(db)
    assert (
        await db.scalar(
            select(RolePermission.id).where(
                RolePermission.role_name == custom.role_name,
                RolePermission.permission == custom.permission,
            )
        )
        == custom.id
    )


async def test_seed_does_not_restore_revoked_default_grant(db: DB) -> None:
    role = "normal:member"
    permission = Permission.content_create
    await seed_rbac(db)
    assert await role_has_permission(db, role, permission)

    assert await set_role_permission(db, role, permission, enabled=False)
    await seed_rbac(db)

    assert not await role_has_permission(db, role, permission)
