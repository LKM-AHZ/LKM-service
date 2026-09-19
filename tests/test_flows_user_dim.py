"""M5 7.2.5：user_dim Prefect flow 的编排与不变量验收。

编排控制流 ``orchestrate_user_dim`` 与 Prefect 解耦：测试注入普通实现函数直接跑，不触碰
Prefect engine（不起临时 server、不撞 filterwarnings=error）；生产则由 flow 注入带重试的
task。底层 ETL 仍复用既有入口，守「命令数恒定 / 跨 realm 双会话 / 回填幂等」。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.model_registry import ensure_all_models
from app.db.user_dim import UserDim
from app.flows.user_dim import (
    _incremental,
    _reconcile_once,
    _sync_ids,
    orchestrate_user_dim,
    user_dim_reconcile_flow,
)
from auth.db.base import auth_metadata
from tests.test_user_dim_sync import _counting
from tests.test_user_dim_wiring_event import (
    _dim,
    _mk_user,
    _use_seam,
)


@pytest.fixture
async def dim_db() -> AsyncIterator[tuple[Any, Any, Any]]:
    """融合 schema 库（Base+auth 同 schema create_all），供 ETL 双会话读写同见。

    与 ``test_user_dim_wiring_event.dim_db`` 同构；本文件自持以避开 pytest fixture
    跨模块导入与参数同名的 lint 歧义。
    """
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


def _plain() -> dict[str, Any]:
    return {
        "reconcile_once": _reconcile_once,
        "incremental": _incremental,
        "sync_ids": _sync_ids,
    }


async def _orchestrate(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("window", 500)
    kwargs.setdefault("user_ids", None)
    kwargs.setdefault("max_rounds", 200)
    return await orchestrate_user_dim(**kwargs, **_plain())


def test_flow_metadata() -> None:
    assert user_dim_reconcile_flow.name == "user-dim-reconcile"
    assert user_dim_reconcile_flow.retries == 1


async def test_ids_backfill_constant_commands(dim_db, monkeypatch) -> None:
    """回填 ids：1 源批读 + 1 批 upsert，命令数恒 2，且只写 dim。"""
    engine, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    u1 = await _mk_user(s, "flowb1", nickname="B1")
    u2 = await _mk_user(s, "flowb2", nickname="B2")
    await s.commit()

    counter = _counting(engine)
    result = await _orchestrate(mode="ids", user_ids=[u1, u2])

    assert result == {"mode": "ids", "updated": 2, "requested": 2}
    assert counter["n"] == 2
    assert (await _dim(s, u1)).nickname == "B1"  # type: ignore[union-attr]
    assert (await _dim(s, u2)).nickname == "B2"  # type: ignore[union-attr]


async def test_ids_backfill_idempotent(dim_db, monkeypatch) -> None:
    """重复回填不产生重复行（upsert 幂等）。"""
    _, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    u1 = await _mk_user(s, "flowi1", nickname="I1")
    await s.commit()

    await _orchestrate(mode="ids", user_ids=[u1])
    await _orchestrate(mode="ids", user_ids=[u1])

    total = (await s.execute(select(func.count()).select_from(UserDim))).scalar_one()
    assert int(total) == 1


async def test_incremental_single_pass_constant_commands(dim_db, monkeypatch) -> None:
    """单拍增量：2 轻扫 + 2 写入 = 恒 4 条。"""
    engine, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    await _mk_user(s, "flowi2", nickname="I2")
    await _mk_user(s, "flowi3", nickname="I3")
    await s.commit()

    counter = _counting(engine)
    result = await _orchestrate(mode="incremental")

    assert result["updated"] == 2
    assert counter["n"] == 4


async def test_reconcile_mode_converges(dim_db, monkeypatch) -> None:
    """reconcile 模式：未物化源被补齐，单拍补行 < window 即收敛。"""
    _, maker, s = dim_db
    _use_seam(monkeypatch, maker)
    u1 = await _mk_user(s, "flowr1", nickname="R1")
    await s.commit()

    result = await _orchestrate(mode="reconcile")

    assert result["mode"] == "reconcile"
    assert result["rounds"] == 1
    assert result["updated"] == 1
    assert (await _dim(s, u1)) is not None


async def test_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        await _orchestrate(mode="nope")
