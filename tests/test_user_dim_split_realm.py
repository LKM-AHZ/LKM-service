"""user_dim ETL 真拆库（跨 realm）路径验收：auth 会话读源 → 业务会话写 dim。

S5 物理拆库后 ``User``/``Profile`` 在 auth 库、``UserDim`` 在业务库。``test_user_dim_sync``
用融合 schema 覆盖批量/命令性质；本文件用 conftest 的 ``auth_db``（AuthBase schema）与
``db``（Base schema）两个**真实分离**会话，验证生产形态下双会话 ETL 真能落库、且不依赖
跨库 join（若误用单会话跨库查询会直接 UndefinedTable）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.user_dim import UserDim
from app.modules.auth.models import Profile, User
from app.modules.auth.user_dim_sync import (
    reconcile_user_dim_incremental,
    refresh_user_dim,
    sync_dim_for_ids,
)


async def _mk_user(
    auth_db: AsyncSession,
    username: str,
    *,
    nickname: str | None = None,
    role: str = "member",
) -> User:
    u = User(
        username=username,
        email=f"{username}@x.com",
        hashed_password="hx",
        account_level="normal",
    )
    auth_db.add(u)
    await auth_db.flush()
    if nickname is not None:
        auth_db.add(Profile(user_id=u.id, nickname=nickname, role=role))
        await auth_db.flush()
    return u


async def _dim(db: AsyncSession, user_id: uuid.UUID) -> UserDim | None:
    return (
        await db.execute(select(UserDim).where(UserDim.user_id == user_id))
    ).scalar_one_or_none()


async def test_sync_reads_auth_writes_business(auth_db: AsyncSession, db: AsyncSession) -> None:
    """源在 auth schema、dim 写在业务 schema——单会话跨库会炸，双会话方成。"""
    u = await _mk_user(auth_db, "realm1", nickname="R1", role="editor")
    await auth_db.commit()

    assert (await sync_dim_for_ids(auth_db, db, [u.id])) == 1
    await db.commit()

    row = await _dim(db, u.id)
    assert row is not None
    assert row.username == "realm1"
    assert row.nickname == "R1"
    assert row.role == "editor"


async def test_refresh_and_reconcile_across_realms(
    auth_db: AsyncSession, db: AsyncSession
) -> None:
    """事件主路单刷 + 周期对账均在真双库下收敛。"""
    u1 = await _mk_user(auth_db, "s1", nickname="S1")
    u2 = await _mk_user(auth_db, "s2", nickname="S2")
    await auth_db.commit()

    assert (await refresh_user_dim(auth_db, db, user_id=u1.id)) == 1
    await db.commit()
    assert (await _dim(db, u1.id)) is not None

    # u2 未物化 → 对账补上 u2；u1 已最新 → 不再重写
    n = await reconcile_user_dim_incremental(auth_db, db, window=10)
    await db.commit()
    assert n == 1
    assert (await _dim(db, u2.id)) is not None

    # 收敛：无未物化/无源变更 → 0
    assert (await reconcile_user_dim_incremental(auth_db, db, window=10)) == 0
