"""M3.B S5 C 段：conftest ``auth_user_uid`` 跨 realm 身份工厂回归锚。

拆库后业务库已无 users 表（users/profiles 迁 AuthBase=18 表）；业务表只引用 auth 库的
裸 uuid user_id。测试须先在 auth 专属 schema 写入真实 User(+Profile) 取稳定 uuid id，
再写回业务 uuid 列。本文件只锚工厂语义本身，在真实双 PG（monolith=lkm / auth=lkm_auth）下可跑。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Profile, User
from tests.conftest import AuthUser, auth_user_uid


async def test_auth_user_uid_mints_id_within_auth_realm(auth_db: AsyncSession) -> None:
    au: AuthUser = await auth_user_uid(
        auth_db, username="alice", nickname="Alice", email="a@x.io"
    )
    # uuid7 主键：工厂 mint 出的 id 是 uuid.UUID（不再有“首用户自 1 对齐”的语义）
    assert isinstance(au.id, uuid.UUID)
    assert au.username == "alice"
    assert au.token  # 有 mint 的 Web Bearer token
    # User + Profile 落 auth 库 schema，同会话可读回
    u = (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()
    assert u.username == "alice"
    p = (await auth_db.execute(select(Profile).where(Profile.user_id == au.id))).scalar_one()
    assert p.nickname == "Alice"
    assert u.account_level == "normal"


async def test_auth_user_uid_ids_are_uuid_and_distinct(auth_db: AsyncSession) -> None:
    a = await auth_user_uid(auth_db, username="a")
    b = await auth_user_uid(auth_db, username="b")
    assert isinstance(a.id, uuid.UUID)
    assert isinstance(b.id, uuid.UUID)
    # uuid 主键下的“不重复”语义：两次创建必得不同 id（原 sequential_ids 的意图）
    assert a.id != b.id
    # 缺省纯功能占位，另验 auth 库建出恰好 2 用户
    assert (await auth_db.scalar(select(func.count()).select_from(User))) == 2
