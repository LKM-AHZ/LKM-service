"""RequirePermission 依赖工厂：授权/未授权返回 cur / 抛 FORBIDDEN。

通过 ``Depends.dependency`` 取到工厂生成的 checker 闭包，绕过 FastAPI 注入链
直接以构造的 CurrentUser + db 调用，验证其判定逻辑。
"""

import uuid

import pytest

from app.core.err import BizError, CommonErr
from app.modules.admin.models import RolePermission
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser
from tests.conftest import DB


def _actor(user_id: uuid.UUID, level: str, role: str) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        account_level=level,
        role=role,
        email=None,
        phone=None,
    )


async def _check(db: DB, user: CurrentUser, perm: Permission) -> CurrentUser:
    dep = RequirePermission(perm)
    checker = dep.dependency  # type: ignore[union-attr]
    return await checker(cur=user, db=db)


async def test_granted_role_passes(db: DB) -> None:
    db.add(
        RolePermission(
            role_name="normal:member", permission=Permission.content_create.value
        )
    )
    await db.flush()
    actor = _actor(uuid.uuid4(), "normal", "member")
    got = await _check(db, actor, Permission.content_create)
    assert got is not None
    assert got.id == actor.id


async def test_ungranted_role_forbidden(db: DB) -> None:
    db.add(
        RolePermission(
            role_name="normal:member", permission=Permission.content_create.value
        )
    )
    await db.flush()
    with pytest.raises(BizError) as exc:
        await _check(
            db,
            _actor(uuid.uuid4(), "normal", "member"),
            Permission.article_owner_comment_delete,
        )
    assert exc.value.errcode == CommonErr.FORBIDDEN


async def test_no_role_row_forbidden(db: DB) -> None:
    with pytest.raises(BizError):
        await _check(db, _actor(uuid.uuid4(), "local", "member"), Permission.content_create)
