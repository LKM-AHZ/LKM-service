"""M3.B0.2 接线验收：user.* 事件 →(失效缓存的同时)写 user_dim；cron 对账任务可跑。

被测是 B0.2 Commit B 的接线层：
1) ``auth.tasks.invalidate_user_snap``(A7 handler，现在既是**在线失效**又是**离线 user_dim
   事件刷新**主路)：把一个真实 user 写进 ETL 会话指向的内存库后调用它，断言 dim 落行、
   且源被字节镜像（改 Profile-only 也更新 dim——A6 盲区由事件而非 updated_at 谓词兜住）。
2) ``auth.tasks.reconcile_user_dim``(周期对账消费口，jobs worker 消费 cron)：对仅存在于源、
   未物化的 user 批扫并落 dim（crash-safety 网）。

seam 指向独立内存库(全量 schema)，避免触碰真实默认 DB；两任务都自开会话、各自 close。
本文件只验"接线能把数据写对目标表"，命令批量性质已在 test_user_dim_sync 由计数证明。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.model_registry import ensure_all_models
from app.db.user_dim import UserDim
from auth.db.base import auth_metadata
from auth.models import Profile, User
from auth.security import hashpwd
from auth.tasks import invalidate_user_snap, reconcile_user_dim


@pytest.fixture
async def dim_db() -> AsyncIterator[tuple[Any, Any, Any]]:
    """自足 PG「融合作业态」库：返回 (engine, sessionmaker, boot-session)。

    S5 拆后 user_dim(base) 与 User/Profile(auth_metadata) 分属两 realm；本测试以单一融合
    schema 同时 create_all Base+auth（无表名冲突），等价“宽表归 auth”收尾态，使 ETL 的
    单会话读写可同时见二者。每连接 search_path=uds(server_settings) 保证稳定。"""
    ensure_all_models()
    url = settings.database_url
    # schema 名含 pid：xdist 并行时同文件用例可能落不同 worker，固定名会互撞
    schema = f"uds_{os.getpid()}"
    boot = create_async_engine(url)
    async with boot.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await boot.dispose()
    engine = create_async_engine(
        url,
        poolclass=StaticPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(auth_metadata.create_all)
    maker = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = maker()
    try:
        yield engine, maker, s
    finally:
        await s.close()
        await engine.dispose()


def _use_seam(monkeypatch: pytest.MonkeyPatch, maker: Any) -> None:
    async def _factory() -> tuple[AsyncSession, AsyncSession]:
        # 融合 schema：源(auth)与目标(user_dim)同库，双会话均指向同一 maker（同一会话对亦可）。
        s = maker()
        return s, s

    monkeypatch.setattr("auth.user_dim_sync._session_factory", _factory)


async def _mk_user(
    session: AsyncSession,
    username: str,
    *,
    nickname: str | None = None,
    role: str = "member",
) -> uuid.UUID:
    u = User(
        username=username,
        email=f"{username}@x.com",
        hashed_password=await hashpwd("secret12345!"),
    )
    session.add(u)
    await session.flush()
    if nickname is not None:
        session.add(Profile(user_id=u.id, nickname=nickname, role=role))
        await session.flush()
    return u.id


async def _dim(session: AsyncSession, uid: uuid.UUID) -> UserDim | None:
    return (
        await session.execute(select(UserDim).where(UserDim.user_id == uid))
    ).scalar_one_or_none()


# (W1) 事件主路：invalidate_user_snap 失效之外同步把真实 user 物化进 user_dim
async def test_user_updated_event_materializes_dim(
    dim_db,
    monkeypatch,
) -> None:
    _, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    uid = await _mk_user(s, "whoami", nickname="初始")
    await s.commit()

    # A7 consumer handler：在线失效(Redis 无 → no-op) + 离线 user_dim 刷新
    await invalidate_user_snap(uid)

    row = await _dim(s, uid)
    assert row is not None
    assert row.username == "whoami"
    assert row.nickname == "初始"


# (W2) Profile-only 变更(A6 盲区：不抬 users.updated_at)也经同一事件把新 nickname 摊进 dim
async def test_user_updated_event_carries_profile_only_change(
    dim_db,
    monkeypatch,
) -> None:
    _, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    uid = await _mk_user(s, "profile_driven", nickname="老")
    await s.commit()
    # 先物化一次(基线)
    await invalidate_user_snap(uid)
    base = await _dim(s, uid)
    assert base is not None and base.nickname == "老"
    # 只改 Profile
    p = (await s.execute(select(Profile).where(Profile.user_id == uid))).scalar_one()
    p.nickname = "事件驱动新名"
    await s.commit()
    # 同一 user.updated 事件刷新 → dim 昵称更新(若只靠 updated_at 谓词会漏, 主路必须兜住)
    await invalidate_user_snap(uid)
    updated = await _dim(s, uid)
    assert updated is not None and updated.nickname == "事件驱动新名"


# (W3) cron 对账网：reconcile_user_dim 批扫未物化源 user 并落 dim(非空集路径)
async def test_periodic_reconcile_materializes_unfamed(
    dim_db,
    monkeypatch,
) -> None:
    _, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    u1 = await _mk_user(s, "r1", nickname="R1")
    u2 = await _mk_user(s, "r2", nickname="R2")
    await s.commit()

    # cron 消费口(auth.tasks.reconcile_user_dim)自开会话落 dim：wrapper 副作用型返回 None，
    # 但底层 periodic 本拍补行=2(未物化源 user 数)。
    from auth.user_dim_sync import reconcile_user_dim_periodic

    assert (await reconcile_user_dim()) is None
    assert (
        await reconcile_user_dim_periodic()
    ) == 0  # wrapper 已全物化 → 二次对账收敛为 0
    r1 = await _dim(s, u1)
    assert r1 is not None and r1.username == "r1" and r1.nickname == "R1"
    assert (await _dim(s, u2)) is not None
