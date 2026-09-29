"""示例数据脚本的跨 realm 边界：演示用户必须造在 **auth 库**，不得由 app 用业务库会话写。

回归背景（2026-09-29 蓝图审计）：`columns` / `files` 两个 seed 原先用**业务库会话**
`db.add(User(...))` 造演示作者——在融合单库下侥幸能跑，物理拆库后业务库没有 users 表，
必 `UndefinedTable`；且违反蓝图 §3.1「主服务绝不直接修改任何 AUTH 数据」。修复后两个 seed
走 auth 缝 `auth.seams.ensure_demo_user`（写操作落在 auth 域），业务行只引用其裸 uuid。

本测的**业务库夹具里根本没有 users 表**，故「seed 仍走业务会话写 users」会直接炸——这条
断言不依赖任何 schema 反射，是修复前必红、修复后必绿的硬信号。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.columns.seed import SEED_COLUMNS, seed_columns
from app.modules.content.models import Column
from app.modules.files.models import LibraryFile
from app.modules.files.seed import SEED_FILES, seed_files
from auth import seed_users
from auth.models import Profile, User


class _SharedSession:
    """借用测试共享 auth 会话跑 ensure_demo_user，但屏蔽 close()。

    ``ensure_demo_user`` 的契约是「自开会话并 close」，而夹具会话由 pytest 拥有——不屏蔽
    close 会把夹具关掉，后续断言就查不到了。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def close(self) -> None:
        return None


def _point_seed_at(monkeypatch: Any, auth_db: AsyncSession) -> None:
    """把 seed 的 auth 会话工厂指向本测 auth 库（database-per-test 克隆）。"""

    async def _factory() -> _SharedSession:
        return _SharedSession(auth_db)

    monkeypatch.setattr(seed_users, "_session_factory", _factory)


async def _assert_no_users_table(db: AsyncSession) -> None:
    """前提守卫：业务库租户里**确实没有** users 表。

    没有这条守卫，本文件的用例可能假绿——若业务库恰好有 users 表（回到融合态），
    「seed 用业务会话写 users」的旧代码也能跑通，回归就不会被发现。守卫红了说明测试
    拓扑不再代表拆库形态，本文件其余断言随之失效。
    """
    found = await db.scalar(text("SELECT to_regclass('users')::text"))
    assert found is None, "业务库不应存在 users 表（拆库拓扑被破坏，本文件断言失效）"


class TestEnsureDemoUser:
    async def should_create_nonloginable_user_with_profile(
        self, auth_db: AsyncSession, monkeypatch: Any
    ):
        _point_seed_at(monkeypatch, auth_db)

        uid = await seed_users.ensure_demo_user(
            username="demo_author", nickname="演示作者"
        )

        user = await auth_db.scalar(select(User).where(User.id == uid))
        assert user is not None
        assert user.username == "demo_author"
        # 哨兵口令：不是任何真实口令的合法哈希 → 该账号无法登录
        assert user.hashed_password == seed_users._SEED_PASSWORD_SENTINEL
        profile = await auth_db.scalar(select(Profile).where(Profile.user_id == uid))
        assert profile is not None
        assert profile.nickname == "演示作者"

    async def should_be_idempotent(self, auth_db: AsyncSession, monkeypatch: Any):
        _point_seed_at(monkeypatch, auth_db)

        first = await seed_users.ensure_demo_user(username="dup", nickname="甲")
        second = await seed_users.ensure_demo_user(username="dup", nickname="乙")

        assert first == second
        rows = (
            (await auth_db.execute(select(User).where(User.username == "dup")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


class TestSeedsTakeOwnerFromAuthRealm:
    async def should_seed_columns_with_auth_realm_owner(
        self, db: AsyncSession, auth_db: AsyncSession, monkeypatch: Any
    ):
        _point_seed_at(monkeypatch, auth_db)
        await _assert_no_users_table(db)

        count = await seed_columns(db)
        assert count == len(SEED_COLUMNS)

        owner = await auth_db.scalar(
            select(User).where(User.username == "column_seed_author")
        )
        assert owner is not None  # 演示作者落在 auth 库

        rows = (await db.execute(select(Column))).scalars().all()
        assert rows
        assert all(r.owner_id == owner.id for r in rows)

        # 幂等：二次执行不再新增
        assert await seed_columns(db) == 0

    async def should_seed_files_with_auth_realm_uploader(
        self, db: AsyncSession, auth_db: AsyncSession, monkeypatch: Any
    ):
        _point_seed_at(monkeypatch, auth_db)
        await _assert_no_users_table(db)

        count = await seed_files(db)
        assert count == len(SEED_FILES)

        uploader = await auth_db.scalar(
            select(User).where(User.username == "file_library_seed_uploader")
        )
        assert uploader is not None

        rows = (await db.execute(select(LibraryFile))).scalars().all()
        assert rows
        assert all(r.uploader_id == uploader.id for r in rows)
