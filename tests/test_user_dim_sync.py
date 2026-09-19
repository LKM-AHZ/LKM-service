"""M3.B0.2 ETL：user_dim 离线宽表填充 + 批量命令计数 验收。

被测对象是 B0.2 的填充腿（``auth.user_dim_sync``：sync_dim_for_ids / refresh_user_dim /
reconcile_user_dim_incremental）。只测"填充/同步"职责——读 auth 源(User/Profile)写 user_dim。
B0.3(报表读侧)与在线一致性(user:snap)都不在此测，且本文件绝不改动源语义。

测试库策略：镜像 test_user_dim_table / test_outbox 的**自足内存引擎** + 显式
``ensure_all_models()``（保证含 user_dim 的全量 metadata 参与 create_all）；并对
``engine.sync_engine`` 挂 ``before_cursor_execute`` 计数——直接在真实内存库上证明
"批量 upsert 是**一条** INSERT ON CONFLICT、命令数与 id 数 N 无关（恒 2：1 读 + 1 写）"。

领域断言：全列字节镜像（nickname/role 来自 profiles，缺失 None；is_banned = bool(is_locked)
与在线缝 snapshot 同义）；upsert 幂等可重跑且改源后更到同 PK 行；离线纪律：sync 只写
user_dim 绝不动源；reconcile 增量收敛、跨库批式命令恒定 4（无候选 2）。

跨 realm 说明：S5 真拆库后源在 auth 库、user_dim 在业务库，入口接收 (source_db, target_db)
双会话；本文件的融合 schema 下传入同一会话两次即可（等价旧融合态），另见
``test_user_dim_split_realm.py`` 对物理双库路径的验收。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import auth.models  # noqa: F401  确保 User/Profile 模型元数据可见
from app.core.config import settings
from app.db.base import Base
from app.db.model_registry import ensure_all_models
from app.db.user_dim import UserDim
from auth.db.base import auth_metadata
from auth.models import Profile, User
from auth.user_dim_sync import (
    reconcile_user_dim_incremental,
    refresh_user_dim,
    sync_dim_for_ids,
)

# 合法 uuid7 形态的不存在 id（sync 应被 join 丢弃）。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


async def _mk_engine():
    """S5 拆后「宽表归属 auth」融合库：单 PG schema 内 create_all Base+auth 双 metadata。

    - 源 User/Profile 在 auth_metadata(AuthBase)；目标 user_dim 在 Base.metadata —— 本测试
      以**融合单一 schema**（无表名冲突）让 ``user_dim_sync`` 的单会话读写同时可见二者，
      等价拆库收尾后的“user_dim 归 auth”终局态（见 admin/dim_report 注释）。
    - 用 ``connect_args.server_settings.search_path`` 让引擎每根连接都落该 schema，跨会话稳定。
    """
    ensure_all_models()
    url = settings.database_url
    schema = "uds"
    # (1) 干净建 schema（默认 schema 连接）
    boot = create_async_engine(url)
    async with boot.begin() as conn:
        await conn.execute(text('DROP SCHEMA IF EXISTS "uds" CASCADE'))
        await conn.execute(text('CREATE SCHEMA "uds"'))
    await boot.dispose()
    # (2) 目标引擎：search_path=uds 后 create_all 双 metadata（Base 业务表 + auth 用户表无冲突）
    eng = create_async_engine(
        url,
        poolclass=StaticPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(auth_metadata.create_all)
    return eng


async def _make_session(engine: Any):
    """由(异步)engine 造会话并建全表。expire_on_commit=False 防 commit 后惰性重载。

    引擎已带 ``server_settings.search_path=uds``，故任何会话自动落隔离 schema；此处再幂等
    create_all Base + auth 双 metadata（供独立调用路径，保证表已就绪）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(auth_metadata.create_all)
    maker = async_sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )
    return maker()


@pytest.fixture
async def DB() -> AsyncIterator[tuple[Any, AsyncSession]]:
    """自足(engine+session)对，供共用一张干净内存库 + 可挂 sync 层事件。"""
    engine = await _mk_engine()
    session = await _make_session(engine)
    try:
        yield engine, session
    finally:
        await session.close()
        await engine.dispose()


def _counting(engine: Any) -> dict[str, int]:
    """给 engine 挂 SQL 语句计数器；返回计数 dict。计数 = before_cursor_execute 次数。"""
    made = {"n": 0}

    def _count(sync_conn: Any, *_: Any, **__: Any) -> None:
        made["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    return made


async def _mk_user(
    session: AsyncSession,
    username: str,
    *,
    nickname: str | None = None,
    role: str = "member",
    account_level: str = "normal",
    email: str | None = None,
    is_locked: bool = False,
) -> User:
    u = User(
        username=username,
        email=email or f"{username}@x.com",
        hashed_password="hx",
        account_level=account_level,
        is_locked=is_locked,
    )
    session.add(u)
    await session.flush()
    if nickname is not None:
        session.add(Profile(user_id=u.id, nickname=nickname, role=role))
        await session.flush()
    return u


async def _dim_row(session: AsyncSession, user_id: uuid.UUID) -> UserDim | None:
    return (
        await session.execute(select(UserDim).where(UserDim.user_id == user_id))
    ).scalar_one_or_none()


async def _dim_count(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(UserDim))).scalar_one()
        or 0
    )


# (1) 单用户 refresh：新建 dim 行 且 全列字节镜像源(含 banned 在线缝语义、profile 缺席)
async def test_refresh_materializes_row_bytes(DB) -> None:
    _e, session = DB
    u = await _mk_user(
        session,
        "alice",
        nickname="Alice",
        role="editor",
        account_level="local",
        is_locked=True,
    )
    await session.commit()

    assert (await refresh_user_dim(session, session, user_id=u.id)) == 1
    row = await _dim_row(session, u.id)
    assert row is not None
    assert row.user_id == u.id
    assert row.username == "alice"
    assert row.email == "alice@x.com"
    assert row.nickname == "Alice"
    assert row.role == "editor"
    assert row.account_level == "local"
    assert row.is_locked is True
    assert row.is_banned is True  # = bool(User.is_locked)，在线缝同义
    assert row.created_at is not None and row.updated_at is not None
    assert row.sync_ts is not None


async def test_refresh_without_profile_is_null_nickname_and_role(DB) -> None:
    _e, session = DB
    u = await _mk_user(session, "noprof", nickname=None)  # 无 Profile 行
    await session.commit()
    await refresh_user_dim(session, session, user_id=u.id)
    row = await _dim_row(session, u.id)
    assert row is not None and row.nickname is None and row.role is None


# (2) upsert 幂等可重跑：源改(含纯 Profile 改，User.updated_at 不动)后更到同 PK 行。
#   ETL 每次由独立离线会话(fresh)消费——这里每一段"改源→刷新"都自开新会话模拟生产，避免
#   复用同一条长生命周期内存会话的 identity map 遮挡真实 DB 权威值(SQLAlchemy async 惰性载)。
async def test_refresh_updates_in_place_incl_banned_flip(DB) -> None:
    engine, session = DB
    u = await _mk_user(session, "bob", nickname="老昵称")
    await session.commit()
    assert (await refresh_user_dim(session, session, user_id=u.id)) == 1
    assert await _dim_count(session) == 1

    # 纯 Profile 改(A6 盲区：不抬 User.updated_at) → 事件主路仍须刷新 dim 昵称(同一PK 行更)
    w1 = await _make_session(engine)
    prof = (
        await w1.execute(select(Profile).where(Profile.user_id == u.id))
    ).scalar_one()
    prof.nickname = "新昵称"
    await w1.commit()
    r1 = await _make_session(engine)
    assert (await refresh_user_dim(r1, r1, user_id=u.id)) == 1
    assert await _dim_count(r1) == 1  # 同 PK 行，不新增
    r1r = await _dim_row(r1, u.id)
    assert r1r is not None and r1r.nickname == "新昵称"
    await w1.close()
    await r1.close()

    # 锁定翻转(纯 User 列) → banned 语义同步(另开 writer 会话翻转 + 新会话刷新)
    w2 = await _make_session(engine)
    u2 = (await w2.execute(select(User).where(User.id == u.id))).scalar_one()
    u2.is_locked = True
    await w2.commit()
    r2 = await _make_session(engine)
    await refresh_user_dim(r2, r2, user_id=u.id)
    flipped = await _dim_row(r2, u.id)
    assert flipped is not None and flipped.is_banned is True
    await w2.close()
    await r2.close()


# (3) 离线纪律：ETL 只写 user_dim，绝不动源
async def test_sync_never_mutates_source(DB) -> None:
    _e, session = DB
    u = await _mk_user(session, "carol", nickname="Car")
    await session.commit()
    await sync_dim_for_ids(session, session, [u.id])
    await session.commit()
    src = (await session.execute(select(User).where(User.id == u.id))).scalar_one()
    assert src.username == "carol"
    assert await _dim_row(session, u.id) is not None


# (4) 真实内存库批量 upsert：sync N id = 恒 2 条 SQL(1 SELECT outer join + 1 INSERT ON CONFLICT)，与 N 无关
async def test_sync_batch_command_count_constant() -> None:
    engine = await _mk_engine()
    session = await _make_session(engine)
    count = _counting(engine)
    try:
        users = []
        for i in range(200):
            u = User(
                username=f"u{i}",
                email=f"u{i}@x.com",
                hashed_password="hx",
                account_level="local",
            )
            session.add(u)
            await session.flush()
            if i % 2 == 0:
                session.add(Profile(user_id=u.id, nickname=f"N{i}", role="member"))
            users.append(u)
        await session.commit()

        count["n"] = 0
        nw = await sync_dim_for_ids(session, session, [u.id for u in users])
        stmts = count["n"]
        assert nw == 200
        assert stmts == 2, (
            f"批量 sync 应恒 2 条命令(1 SELECT + 1 INSERT ON CONFLICT), 实得 {stmts}"
        )
        assert await _dim_count(session) == 200
    finally:
        await session.close()
        await engine.dispose()


# (5) 幂等重跑=同 PK 更新：重跑仍 2 命令、行数不变、改源后更到新值
async def test_sync_batch_idempotent_reupdate(DB) -> None:
    engine, session = DB
    count = _counting(engine)
    u = await _mk_user(session, "dup", nickname="D")
    await session.commit()
    await sync_dim_for_ids(session, session, [u.id])
    await session.commit()
    # 建 2 个 id 批量(含不存在 id, 应被 join 丢弃只 upd 真存在者)
    count["n"] = 0
    assert (await sync_dim_for_ids(session, session, [u.id, _MISSING_ID])) == 1
    assert count["n"] == 2
    p = (
        await session.execute(select(Profile).where(Profile.user_id == u.id))
    ).scalar_one()
    p.nickname = "D2"
    await session.commit()
    await sync_dim_for_ids(session, session, [u.id])
    assert (await _dim_row(session, u.id)).nickname == "D2"
    assert await _dim_count(session) == 1


# (6) reconcile 增量：只补"未物化 或 源 User 列自上次写点后变更"者；穷举后收敛返 0
async def test_reconcile_catches_new_and_stale_then_converges(DB) -> None:
    _e, session = DB
    untouched = await _mk_user(session, "t1", nickname="T1")
    stale = await _mk_user(session, "s1", nickname="S1")
    newbie = await _mk_user(session, "n1", nickname="N1")
    await session.commit()
    # 先物化 untouched/stale → 其 sync_ts≈now；newbie 刻意留作"未物化"
    await sync_dim_for_ids(session, session, [untouched.id, stale.id])
    await session.commit()
    # 对 stale 改纯 User 列(username)抬 updated_at，制造源变更
    su = (await session.execute(select(User).where(User.id == stale.id))).scalar_one()
    su.username = "s1_renamed"
    await session.commit()

    await reconcile_user_dim_incremental(session, session, window=10)
    stale_row = await _dim_row(session, stale.id)
    assert stale_row is not None
    assert stale_row.username == "s1_renamed"
    assert (await _dim_row(session, newbie.id)) is not None  # 未物化被补
    # untouched 未变(不重写)，仍物化、不变动
    untouched_row = await _dim_row(session, untouched.id)
    assert untouched_row is not None
    assert untouched_row.nickname == "T1"

    # 收敛：无"变更/未物化"者，再来返 0
    assert (await reconcile_user_dim_incremental(session, session, window=10)) == 0


# (7) reconcile 批式命令计数恒定(=3)于真实内存库；空集直接返回只发 1 条候选 SELECT
async def test_reconcile_command_count_constant() -> None:
    engine = await _mk_engine()
    session = await _make_session(engine)
    count = _counting(engine)
    try:
        for i in range(60):
            await _mk_user(session, f"r{i}", nickname=f"R{i}")
        await session.commit()
        # 60 个全未物化：首拍 window=50 补 50，命令恒定 4(跨库双扫描 + sync 的读/写 2 条)
        count["n"] = 0
        assert (await reconcile_user_dim_incremental(session, session, window=50)) == 50
        assert count["n"] == 4, f"reconcile 跨库批式应恒 4 命令, 实得 {count['n']}"
        # 余 10 个仍 4 命令
        count["n"] = 0
        assert (await reconcile_user_dim_incremental(session, session, window=50)) == 10
        assert count["n"] == 4
        # 全收敛：候选为空 → 直接返回 0，只有 2 条扫描(auth + 业务)查询判定
        count["n"] = 0
        assert (await reconcile_user_dim_incremental(session, session, window=50)) == 0
        assert count["n"] == 2
    finally:
        await session.close()
        await engine.dispose()
