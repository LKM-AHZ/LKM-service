"""require_owner 底层谓词 check_owner：admin/属主/他人三分支。"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.modules.admin.models import RolePermission
from app.modules.content.models import Board, ContentItem
from app.modules.rbac.permissions import DEFAULT_GRANTS, Permission
from app.modules.rbac.service import check_owner
from auth.deps import CurrentUser
from auth.models import User
from tests.conftest import DB


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """rbac deps 用例需 auth(users)+biz(content/role_permissions) 单 schema（融合装配）。"""
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seed_grants(db: DB) -> None:
    """将默认角色→权限映射落库，否则 role_has_permission 一律为空：
    admin 分支无从验证（admin:super_admin 需 content_owner_delete 授权）。"""
    for role, grants in DEFAULT_GRANTS.items():
        for g in grants:
            db.add(RolePermission(role_name=role, permission=g.permission.value))
    await db.flush()


def _actor(user_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        account_level="normal",
        role="member",
        email=None,
        phone=None,
    )


def _admin(user_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        account_level="admin",
        role="super_admin",
        email=None,
        phone=None,
    )


async def _mk_post(db: DB) -> tuple[uuid.UUID, uuid.UUID]:
    """建真实 board + 真实作者 user，返回 (post_id, author_id)。

    content_items.board_id/author_id 是强 FK（board NOT NULL）；裸插不存在的 board_id /
    author_id 在 PG 是孤儿 FK 会失败。返回落库 uuid，属主判定用真实作者 id。
    """
    board = Board(slug="b", title="Board", description="")
    db.add(board)
    await db.flush()
    author = User(username="author", hashed_password="x")
    db.add(author)
    await db.flush()
    post = ContentItem(
        content_type="discussion",
        board_id=board.id,
        author_id=author.id,
        title="t",
        content="c",
    )
    db.add(post)
    await db.flush()
    return post.id, author.id


async def test_owner_allowed_by_id(db: DB) -> None:
    pid, aid = await _mk_post(db)
    await check_owner(
        db, _actor(aid), pid, ContentItem, "author_id", Permission.content_owner_delete
    )
    # 无异常即通过


async def test_foreign_forbidden(db: DB) -> None:
    pid, _ = await _mk_post(db)
    # 非属主 id 必须不同于作者 id（也不落库，仅为比较）
    stranger = uuid.uuid4()
    with pytest.raises(BizError) as exc:
        await check_owner(
            db,
            _actor(stranger),
            pid,
            ContentItem,
            "author_id",
            Permission.content_owner_delete,
        )
    assert exc.value.errcode == CommonErr.FORBIDDEN


async def test_admin_allowed(db: DB) -> None:
    pid, _ = await _mk_post(db)
    await check_owner(
        db,
        _admin(uuid.uuid4()),
        pid,
        ContentItem,
        "author_id",
        Permission.content_owner_delete,
    )


async def test_admin_requires_grant(db: DB) -> None:
    # admin 但如果 role 未被授予该 object 权限点（例如 admin:org_member 无 content_owner_delete）则仍拒
    pid, _ = await _mk_post(db)
    org = CurrentUser(
        id=uuid.uuid4(),
        account_level="admin",
        role="org_member",
        email=None,
        phone=None,
    )
    with pytest.raises(BizError):
        await check_owner(
            db, org, pid, ContentItem, "author_id", Permission.content_owner_delete
        )
