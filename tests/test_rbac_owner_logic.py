"""对象级授权的短路和资源存在性，不依赖外部数据库。"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Column, DateTime, MetaData, Table, Uuid

from app.modules.content.models import ContentItem
from app.modules.rbac.permissions import Permission
from app.modules.rbac.repository import ResourceRepository
from app.modules.rbac.service import check_owner
from core.contracts import CurrentUser
from core.err import BizError, CommonErr


def _actor(user_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(id=user_id, account_level="normal", role="member")


async def test_owner_query_selects_only_owner_and_excludes_deleted() -> None:
    table = Table(
        "test_resources",
        MetaData(),
        Column("id", Uuid, primary_key=True),
        Column("author_id", Uuid),
        Column("deleted_at", DateTime(timezone=True)),
    )
    resource = SimpleNamespace(
        id=table.c.id,
        author_id=table.c.author_id,
        deleted_at=table.c.deleted_at,
    )
    db = MagicMock()
    result = MagicMock()
    result.first.return_value = (uuid.uuid4(),)
    db.execute = AsyncMock(return_value=result)

    await ResourceRepository(db).get_owner_row(resource, uuid.uuid4(), "author_id")

    stmt = db.execute.await_args.args[0]
    sql = str(stmt)
    assert "SELECT test_resources.author_id" in sql
    assert "test_resources.deleted_at IS NULL" in sql


async def test_owner_skips_role_permission_lookup(monkeypatch) -> None:
    owner_id = uuid.uuid4()
    lookup = AsyncMock(return_value=(owner_id,))
    permission = AsyncMock()
    monkeypatch.setattr(ResourceRepository, "get_owner_row", lookup)
    monkeypatch.setattr("app.modules.rbac.service.role_has_permission", permission)

    await check_owner(
        None,
        _actor(owner_id),
        uuid.uuid4(),
        ContentItem,
        "author_id",
        Permission.content_owner_delete,
    )

    lookup.assert_awaited_once()
    permission.assert_not_awaited()


async def test_missing_resource_cannot_be_authorized_by_role(monkeypatch) -> None:
    lookup = AsyncMock(return_value=None)
    permission = AsyncMock(return_value=True)
    monkeypatch.setattr(ResourceRepository, "get_owner_row", lookup)
    monkeypatch.setattr("app.modules.rbac.service.role_has_permission", permission)

    with pytest.raises(BizError) as exc:
        await check_owner(
            None,
            _actor(uuid.uuid4()),
            uuid.uuid4(),
            ContentItem,
            "author_id",
            Permission.content_owner_delete,
        )

    assert exc.value.errcode == CommonErr.FORBIDDEN
    permission.assert_not_awaited()


@pytest.mark.parametrize("granted", [True, False])
async def test_non_owner_requires_role_permission(monkeypatch, granted: bool) -> None:
    lookup = AsyncMock(return_value=(uuid.uuid4(),))
    permission = AsyncMock(return_value=granted)
    monkeypatch.setattr(ResourceRepository, "get_owner_row", lookup)
    monkeypatch.setattr("app.modules.rbac.service.role_has_permission", permission)

    call = check_owner(
        None,
        _actor(uuid.uuid4()),
        uuid.uuid4(),
        ContentItem,
        "author_id",
        Permission.content_owner_delete,
    )
    if granted:
        await call
    else:
        with pytest.raises(BizError) as exc:
            await call
        assert exc.value.errcode == CommonErr.FORBIDDEN

    permission.assert_awaited_once()
