"""模块5：init_db 多 worker 迁移锁（Redis 串行化；不可用 fail-open）。"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.core.redis as redis_mod
from app.core.config import settings
from app.db import init_db as init_db_mod


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


@pytest.fixture(autouse=True)
async def _mock_seed(monkeypatch: Any) -> AsyncIterator[None]:
    """隔离 RBAC seed 的 DB 副作用：锁测试只验证迁移锁行为。

    init_db 现在会调 _seed_base_data（种默认权限），它内部 new_session() 连的是
    真实配置库，会污染开发库并拖慢锁测试；故这里 mock 为 no-op，seed 正确性由
    test_rbac_seed.py 与 test_invokes_rbac_seed（恢复真实实现）覆盖。
    """
    if not hasattr(init_db_mod, "_REAL_seed_base_data"):
        init_db_mod._REAL_seed_base_data = init_db_mod._seed_base_data

    async def _noop() -> None:
        return None

    monkeypatch.setattr(init_db_mod, "_seed_base_data", _noop)
    yield


async def _enable_fake_redis(monkeypatch: Any) -> Any:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis()

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    return fake


async def test_lock_acquired_when_redis_available(monkeypatch) -> None:
    """Redis 可用且无竞争者 → 抢到锁，init_db 正常跑迁移并释放。"""
    fake = await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "use_alembic", True)
    ran = 0

    def _fake_ide() -> None:
        nonlocal ran
        ran += 1

    monkeypatch.setattr(init_db_mod, "_run_upgrade", _fake_ide)
    await init_db_mod.init_db()
    assert ran == 1
    # 迁移结束锁已释放（键不存在或已被删）
    held = await fake.get(init_db_mod._MIGRATION_LOCK_KEY)
    assert held is None


async def test_lock_serializes_waiting_worker(monkeypatch) -> None:
    """已有 worker 持锁 → 当前 worker 等待；锁释放后重新抢占再迁移。"""
    fake = await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "use_alembic", True)
    # 预置一把锁，模拟另一个 worker 正在迁移
    await fake.set(init_db_mod._MIGRATION_LOCK_KEY, "1", nx=True)
    ran = 0

    def _fake_ide() -> None:
        nonlocal ran
        ran += 1

    monkeypatch.setattr(init_db_mod, "_run_upgrade", _fake_ide)
    # 持锁方中途释放，使等待者能抢占
    original_sleep = init_db_mod.asyncio.sleep

    async def _release_after_sleep(_d: float) -> None:
        await original_sleep(0)
        await fake.delete(init_db_mod._MIGRATION_LOCK_KEY)

    monkeypatch.setattr(init_db_mod.asyncio, "sleep", _release_after_sleep)
    await init_db_mod.init_db()
    assert ran == 1


async def test_invokes_rbac_seed(monkeypatch) -> None:
    """schema 就绪后 init_db 恒调用 RBAC seed（种子权限映射，取代人工 CLI）。"""
    # 恢复 autouse _mock_seed 替换掉的真实实现，验证 seed 真实落库
    monkeypatch.setattr(
        init_db_mod, "_seed_base_data", init_db_mod._REAL_seed_base_data
    )
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import StaticPool

    import app.db.session as db_session
    from app.db.base import Base
    from app.modules.admin.models import RolePermission
    from app.modules.rbac.permissions import DEFAULT_GRANTS

    # 建一个隔离 PG schema（业务库），StaticPool 单连接 + SET search_path → 该连接所有
    # 会话（含 seed 落库）都落此 schema，避免误写开发库；测毕 drop。模式与 conftest 一致。
    schema = "s_rbac_seed"
    engine = create_async_engine(settings.database_url, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )
    session: AsyncSession = SessionLocal()

    async def _fake_new_session() -> AsyncSession:
        # 同一引擎（StaticPool 已 SET search_path）→ 新会话仍落在该 schema
        return SessionLocal()

    monkeypatch.setattr(db_session, "new_session", _fake_new_session)

    async def _noop_create_all() -> None:
        return None

    monkeypatch.setattr(init_db_mod, "_create_all", _noop_create_all)
    # use_alembic 默认 False → 走 create_all 分支 + seed
    await init_db_mod.init_db()

    await session.execute(text(f'SET search_path TO "{schema}"'))
    total = (
        await session.execute(select(func.count()).select_from(RolePermission))
    ).scalar_one()
    expected = sum(len(v) for v in DEFAULT_GRANTS.values())
    assert total == expected
    await session.close()
    await engine.dispose()

    # drop 该一次性 schema
    _clean = create_async_engine(settings.database_url)
    async with _clean.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await _clean.dispose()


async def test_fail_open_when_redis_disabled(monkeypatch) -> None:
    """Redis 未配置 → 不设锁直接跑（dev 单 worker 场景）。"""
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "use_alembic", True)
    ran = 0

    def _fake_ide() -> None:
        nonlocal ran
        ran += 1

    monkeypatch.setattr(init_db_mod, "_run_upgrade", _fake_ide)
    await init_db_mod.init_db()
    assert ran == 1


# ─────────────────────────────────────────────────────────────────────────────
# AUTH 独立库 schema 初始化（M3.B 拆库后 auth 进程自持；见 init_auth_db）
# ─────────────────────────────────────────────────────────────────────────────


async def test_init_auth_db_create_all_channel(monkeypatch) -> None:
    """use_alembic=False → 走 AuthBase.create_all 通道，不触第二迁移链。"""
    monkeypatch.setattr(settings, "use_alembic", False)
    calls: list[str] = []

    async def _fake_create() -> None:
        calls.append("create_all")

    def _fake_upgrade() -> None:
        calls.append("upgrade")

    monkeypatch.setattr(init_db_mod, "_create_auth_all", _fake_create)
    monkeypatch.setattr(init_db_mod, "_run_auth_upgrade", _fake_upgrade)
    await init_db_mod.init_auth_db()
    assert calls == ["create_all"]


async def test_init_auth_db_alembic_channel_uses_auth_lock(monkeypatch) -> None:
    """use_alembic=True → 走 alembic_auth 第二链，且用独立的 auth 迁移锁。"""
    fake = await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "use_alembic", True)
    calls: list[str] = []

    async def _fake_create() -> None:
        calls.append("create_all")

    def _fake_upgrade() -> None:
        calls.append("upgrade")

    monkeypatch.setattr(init_db_mod, "_create_auth_all", _fake_create)
    monkeypatch.setattr(init_db_mod, "_run_auth_upgrade", _fake_upgrade)
    await init_db_mod.init_auth_db()
    assert calls == ["upgrade"]
    # 锁已释放；且业务链 key 未被本通道占用/误删
    assert await fake.get(init_db_mod._AUTH_MIGRATION_LOCK_KEY) is None


async def test_create_auth_all_builds_all_auth_tables(monkeypatch) -> None:
    """通道落库：AuthBase.create_all 在 auth 库建出全部 18 张 auth 表。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.db.auth_session as auth_session_mod
    from app.db.auth_base import auth_metadata

    schema = "s_auth_init"
    engine = create_async_engine(settings.auth_database_url, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))

    # _create_auth_all 内部 import get_auth_engine → patch 模块属性即生效
    monkeypatch.setattr(auth_session_mod, "get_auth_engine", lambda: engine)
    try:
        await init_db_mod._create_auth_all()
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
                    ),
                    {"s": schema},
                )
            ).scalar_one()
        assert n == len(auth_metadata.tables) == 18
    finally:
        await engine.dispose()
        clean = create_async_engine(settings.auth_database_url)
        async with clean.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await clean.dispose()


async def test_auth_metadata_disjoint_from_business_base() -> None:
    """拆库不变量：auth 表（users/profiles…）只挂 AuthBase，不进业务 Base.metadata。"""
    from app.db.auth_base import auth_metadata
    from app.db.base import Base
    from app.db.model_registry import ensure_all_models

    ensure_all_models()
    assert set(auth_metadata.tables).isdisjoint(Base.metadata.tables)
    assert "users" in auth_metadata.tables
    assert "users" not in Base.metadata.tables


def test_auth_alembic_chain_baseline_head() -> None:
    """alembic_auth 第二链存在唯一基线 head（空 versions 目录会让 upgrade 空跑）。"""
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(Config(str(repo_root / "alembic.auth.ini")))
    assert script.get_current_head() == "a0b1c2d3e4f5"
