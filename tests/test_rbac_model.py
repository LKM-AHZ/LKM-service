"""role_permissions 表结构。"""

import pytest
from sqlalchemy import select

from app.modules.admin.models import RolePermission
from tests.conftest import DB


async def test_role_permission_unique(db: DB) -> None:
    db.add(RolePermission(role_name="admin:super_admin", permission="admin.dashboard"))
    await db.flush()
    db.add(RolePermission(role_name="admin:super_admin", permission="admin.dashboard"))
    with pytest.raises(Exception):  # noqa: B017  # UniqueConstraint
        await db.flush()


async def test_role_permission_columns(db: DB) -> None:
    row = RolePermission(role_name="normal:member", permission="content.create")
    db.add(row)
    await db.flush()
    got = (await db.execute(select(RolePermission))).scalars().one()
    assert got.role_name == "normal:member"
    assert got.permission == "content.create"
    assert got.enabled is True
    assert got.id is not None
