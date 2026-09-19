"""Repository 泛型基类（``app/db/repository.py``）行为契约。

覆盖三类容易被后续改动破坏的契约：

1. **CRUD 语义**：get/get_one/get_many/count/exists/create/update/delete 的条件与分页；
2. **软删动态判定**：模型有 ``deleted_at`` 列才过滤、``include_deleted=True`` 是逃生口、
   无该列的模型上 ``soft_delete`` 退化为硬删且 ``restore`` 是空操作；
3. **事务归属**：仓储只 flush，**永不 commit**——最外层会话仍是唯一提交主体。
"""

import uuid

import pytest

from app.core.err import BizError, CommonErr
from app.db.repository import AsyncRepository
from app.modules.admin.models import RolePermission
from app.modules.content.models import Board
from app.modules.notification.models import Notification, NotificationPreference
from app.modules.starhope.models import StarHopeFolder
from tests.conftest import DB


class NotificationRepository(AsyncRepository[Notification]):
    model = Notification


class PreferenceRepository(AsyncRepository[NotificationPreference]):
    model = NotificationPreference


class StarHopeFolderRepository(AsyncRepository[StarHopeFolder]):
    model = StarHopeFolder


class BoardRepository(AsyncRepository[Board]):
    """无 ``deleted_at`` 列的对照模型。"""

    model = Board


async def _make_notification(
    db: DB, user_id: uuid.UUID, note_type: str
) -> Notification:
    return await NotificationRepository(db).create(
        user_id=user_id, type=note_type, payload={"k": note_type}
    )


# ─────────────────────── 基础 CRUD ───────────────────────


async def test_create_flushes_and_assigns_pk(db: DB) -> None:
    user_id = uuid.uuid4()
    note = await _make_notification(db, user_id, "system")
    # uuid_generate_v7() 的 server_default 在 flush 后已回填，无需 commit 即可读主键
    assert isinstance(note.id, uuid.UUID)
    assert await NotificationRepository(db).get(note.id) is note


async def test_get_or_raise_missing(db: DB) -> None:
    repo = NotificationRepository(db)
    assert await repo.get(uuid.uuid4()) is None
    with pytest.raises(BizError):
        await repo.get_or_raise(uuid.uuid4(), CommonErr.INVALID_INPUT)


async def test_get_one_and_get_many(db: DB) -> None:
    user_id = uuid.uuid4()
    first = await _make_notification(db, user_id, "a")
    second = await _make_notification(db, user_id, "b")
    repo = NotificationRepository(db)

    assert await repo.get_one(Notification.id == second.id) is second
    assert await repo.get_one(Notification.id == uuid.uuid4()) is None

    rows = await repo.get_many(
        Notification.user_id == user_id, order_by=Notification.id.asc()
    )
    assert [r.id for r in rows] == [first.id, second.id]

    # git 游标语义：uuid7 时间有序，offset/limit 切页与 order_by(id) 一致
    tail = await repo.get_many(
        Notification.user_id == user_id,
        order_by=Notification.id.asc(),
        offset=1,
        limit=1,
    )
    assert [r.id for r in tail] == [second.id]


async def test_count_and_exists(db: DB) -> None:
    user_id = uuid.uuid4()
    await _make_notification(db, user_id, "a")
    await _make_notification(db, user_id, "b")
    repo = NotificationRepository(db)

    assert await repo.count(Notification.user_id == user_id) == 2
    assert await repo.count(Notification.user_id == uuid.uuid4()) == 0
    assert await repo.exists(Notification.user_id == user_id) is True
    assert await repo.exists(Notification.id == uuid.uuid4()) is False


async def test_update_and_bulk_update_where(db: DB) -> None:
    user_id = uuid.uuid4()
    note = await _make_notification(db, user_id, "a")
    repo = NotificationRepository(db)

    await repo.update(note, type="renamed")
    assert note.type == "renamed"
    assert (await repo.get(note.id)).type == "renamed"

    affected = await repo.update_where(
        {"type": "bulk"}, Notification.user_id == user_id
    )
    assert affected == 1
    assert (await repo.get(note.id)).type == "bulk"


async def test_delete_and_hard_delete_where(db: DB) -> None:
    user_id = uuid.uuid4()
    first = await _make_notification(db, user_id, "a")
    await _make_notification(db, user_id, "b")
    repo = NotificationRepository(db)

    await repo.delete(first)
    assert await repo.get(first.id) is None
    assert await repo.hard_delete_where(Notification.user_id == user_id) == 1
    assert await repo.count() == 0


# ─────────────────────── 软删除 ───────────────────────


async def test_soft_delete_filters_by_default(db: DB) -> None:
    user_id = uuid.uuid4()
    folder = await StarHopeFolderRepository(db).create(
        id=str(uuid.uuid4()), user_id=user_id, name="f"
    )
    repo = StarHopeFolderRepository(db)

    await repo.soft_delete(folder)
    assert folder.deleted_at is not None

    assert await repo.get(folder.id) is None
    assert await repo.get(folder.id, include_deleted=True) is folder
    assert await repo.count(StarHopeFolder.user_id == user_id) == 0
    assert (
        await repo.count(StarHopeFolder.user_id == user_id, include_deleted=True) == 1
    )
    assert await repo.exists(StarHopeFolder.user_id == user_id) is False
    assert await repo.get_many(StarHopeFolder.user_id == user_id) == []
    assert await repo.get_one(StarHopeFolder.user_id == user_id) is None
    with pytest.raises(BizError):
        await repo.get_or_raise(folder.id, CommonErr.INVALID_INPUT)


async def test_restore_brings_row_back(db: DB) -> None:
    user_id = uuid.uuid4()
    folder = await StarHopeFolderRepository(db).create(
        id=str(uuid.uuid4()), user_id=user_id, name="f"
    )
    repo = StarHopeFolderRepository(db)

    await repo.soft_delete(folder)
    await repo.restore(folder)
    assert folder.deleted_at is None
    assert await repo.get(folder.id) is folder


async def test_soft_delete_where_skips_already_deleted(db: DB) -> None:
    user_id = uuid.uuid4()
    repo = StarHopeFolderRepository(db)
    await repo.create(id=str(uuid.uuid4()), user_id=user_id, name="a")
    await repo.create(id=str(uuid.uuid4()), user_id=user_id, name="b")

    assert await repo.soft_delete_where(StarHopeFolder.user_id == user_id) == 2
    # 二次调用不再命中原先已删的行（WHERE deleted_at IS NULL）
    assert await repo.soft_delete_where(StarHopeFolder.user_id == user_id) == 0
    assert (
        await repo.count(StarHopeFolder.user_id == user_id, include_deleted=True) == 2
    )


async def test_model_without_deleted_at_column(db: DB) -> None:
    """无 ``deleted_at`` 列：过滤零副作用，soft_delete 退化为硬删。"""
    repo = BoardRepository(db)
    board = await repo.create(slug=f"b{uuid.uuid4().hex[:8]}", title="t")

    assert repo.soft_delete_column is None
    assert await repo.get(board.id) is board
    assert await repo.count() == 1

    await repo.soft_delete(board)
    assert await repo.count() == 0  # 无列 → 无过滤可加，行确实没了

    survivor = await repo.create(slug=f"b{uuid.uuid4().hex[:8]}", title="t")
    await repo.restore(survivor)  # 空操作
    assert await repo.get(survivor.id) is survivor


# ─────────────────────── upsert ───────────────────────


async def test_pg_upsert_do_nothing(db: DB) -> None:
    user_id = uuid.uuid4()
    repo = PreferenceRepository(db)
    values = {"user_id": user_id, "type": "reply", "enabled": True}

    await repo.pg_upsert(values, index_elements=["user_id", "type"], do_nothing=True)
    conflicting = {
        "user_id": user_id,
        "type": "reply",
        "enabled": False,
    }
    await repo.pg_upsert(
        conflicting, index_elements=["user_id", "type"], do_nothing=True
    )
    assert await repo.count() == 1
    kept = await repo.get_one(
        NotificationPreference.user_id == user_id,
        NotificationPreference.type == "reply",
    )
    assert kept.enabled is True  # 冲突时保留原值


async def test_pg_upsert_update_on_conflict(db: DB) -> None:
    user_id = uuid.uuid4()
    repo = PreferenceRepository(db)
    await repo.pg_upsert(
        {"user_id": user_id, "type": "reply", "enabled": True},
        index_elements=["user_id", "type"],
        update_columns=["enabled"],
    )
    await repo.pg_upsert(
        {"user_id": user_id, "type": "reply", "enabled": False},
        index_elements=["user_id", "type"],
        update_columns=["enabled"],
    )
    assert await repo.count() == 1
    row = await repo.get_one(NotificationPreference.user_id == user_id)
    assert row.enabled is False  # 冲突时取本次提议值（excluded）


async def test_pg_upsert_update_values_constant(db: DB) -> None:
    user_id = uuid.uuid4()
    repo = PreferenceRepository(db)
    await repo.pg_upsert(
        {"user_id": user_id, "type": "reply", "enabled": True},
        index_elements=["user_id", "type"],
        update_values={"enabled": True},
    )
    await repo.pg_upsert(
        {"user_id": user_id, "type": "reply", "enabled": False},
        index_elements=["user_id", "type"],
        update_values={"enabled": True},
    )
    row = await repo.get_one(NotificationPreference.user_id == user_id)
    assert row.enabled is True  # 显式常量覆盖本次提议值


# ─────────────────────── 事务归属 ───────────────────────


async def test_repository_never_commits(db: DB) -> None:
    """写路径只 flush：调用后会话事务仍开着，提交主体仍是最外层依赖。"""
    repo = StarHopeFolderRepository(db)
    folder = await repo.create(id=str(uuid.uuid4()), user_id=uuid.uuid4(), name="f")
    await repo.update(folder, name="g")
    await repo.soft_delete(folder)
    await repo.soft_delete_where(StarHopeFolder.id == folder.id)
    await repo.hard_delete_where(StarHopeFolder.id == folder.id)
    await PreferenceRepository(db).pg_upsert(
        {"user_id": uuid.uuid4(), "type": "reply", "enabled": True},
        index_elements=["user_id", "type"],
        do_nothing=True,
    )
    await repo.flush()

    assert db.in_transaction() is True


async def test_role_permission_composite_pk_model(db: DB) -> None:
    """基类不依赖 ``id`` 列：复合主键模型只能用条件式方法（pk_attr 不可解析）。"""
    repo = AsyncRepository[RolePermission](db)
    repo.model = RolePermission  # 运行期绑定，模拟动态模型缝
    db.add(RolePermission(role_name="r", permission="p"))
    await repo.flush()
    assert await repo.exists(RolePermission.role_name == "r") is True
