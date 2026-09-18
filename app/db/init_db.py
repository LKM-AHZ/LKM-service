"""数据库初始化 —— Alembic 为 schema 唯一权威。

多 worker安全：每个 uvicorn worker 的 lifespan 都会调 init_db()。
首次建库时并发 upgrade 会有竞态（重复建表/版本锁冲突），故用 Redis 分布式锁串行化；
Redis 不可用（未配置/宕机，fail-open）则不设锁直接跑（dev 单 worker 本无并发）。

另有 :func:`init_auth_db`：**auth 独立库**的 schema 初始化（M3.B 真拆库后 auth 表挂
``AuthBase``/``auth_metadata``，与业务库 ``Base.metadata`` 分属两个 PG 库）。按
「进程=库边界」原则由 auth 进程（``app.main_auth``）自持，单体的 ``init_db`` 不触达 auth 库。
两把迁移锁按库分 key，避免业务/auth 迁移互相阻塞。
"""

import asyncio
import logging
from contextlib import suppress
from typing import Any

logger = logging.getLogger("lkm.init_db")

_MIGRATION_LOCK_KEY = "lkm:migration:lock"
_AUTH_MIGRATION_LOCK_KEY = "lkm:migration:auth:lock"
_MIGRATION_LOCK_TTL = 120  # 秒：迁移超时上限后锁自动过期
_MIGRATION_LOCK_WAIT = 8  # 秒：拿不到锁时最多等待的时长
_MIGRATION_LOCK_POLL = 0.3  # 轮询间隔


def _run_upgrade() -> None:
    """在独立线程里同步执行 Alembic upgrade head。

    env.py 的在线迁移内部用 ``asyncio.run`` 创建事件循环，而 init_db 在
    FastAPI lifespan（已运行的事件循环）中被调用，直接调用 command.upgrade
    会因 "cannot be called from a running event loop" 崩溃，故放到线程池。
    """
    from pathlib import Path

    from alembic.config import Config

    from alembic import command

    # 复用后端仓库根下的 alembic.ini（含 script_location 与 env.py），
    # 迁移沿用 env.py 的 sqlalchemy.url（来自 settings），不在此覆盖。
    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    command.upgrade(cfg, "head")


async def _acquire_migration_lock(key: str = _MIGRATION_LOCK_KEY) -> bool:
    """用 Redis SET NX 抢迁移锁；未配置/失败返回 False（fail-open 不设锁）。"""
    from app.core import redis as redis_client

    client = await redis_client.get_redis()
    if client is None:
        return False
    try:
        ok = bool(await client.set(key, "1", nx=True, ex=_MIGRATION_LOCK_TTL))
        if ok:
            return True
        # 拿不到 → 有别的 worker 在迁移：轮询等待其释放
        waited = 0.0
        while waited < _MIGRATION_LOCK_WAIT:
            await asyncio.sleep(_MIGRATION_LOCK_POLL)
            waited += _MIGRATION_LOCK_POLL
            # 对方已释放并成功重新抢占（lock 已过期）→ 自己来迁
            gone = bool(await client.get(key)) is False
            if gone and bool(
                await client.set(key, "1", nx=True, ex=_MIGRATION_LOCK_TTL)
            ):
                return True
        return False  # 等待超时：照常跑（幂等 no-op）
    except Exception:
        return False  # Redis 异常 → fail-open


async def _release_migration_lock(
    held: bool, key: str = _MIGRATION_LOCK_KEY
) -> None:
    if not held:
        return
    from app.core import redis as redis_client

    client = await redis_client.get_redis()
    if client is None:
        return
    with suppress(Exception):
        await client.delete(key)


def _sync_additive_schema(conn: Any) -> list[str]:
    """为**已存在的表**补上 metadata 里新增的列与索引（只增不改，幂等）。

    为什么需要：``create_all`` 只建缺失的**表**，对已存在的表是 no-op——于是「加列型」
    变更在 ``LKM_USE_ALEMBIC=false``（compose 默认）的**既有部署**上升级后不会生效，
    新代码一查新列就 ``UndefinedColumn``。本函数把这类加性变更加进 create_all 通道。

    边界（刻意保守）：只做 ``ADD COLUMN IF NOT EXISTS`` 与 ``CREATE INDEX``（缺则建），
    **绝不**改类型、删列、改约束——破坏性 schema 变更仍必须走 alembic 人工评审。
    因此它与 alembic 是「加性兜底」而非替代。
    """
    import sqlalchemy as sa
    from sqlalchemy.schema import CreateColumn, CreateIndex

    from app.db.base import Base

    changed: list[str] = []
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # 用 tables.values() 而非 sorted_tables：本函数只做加列/加索引，不需要拓扑序，
    # 而 sorted_tables 会因 qa_answers/qa_questions 的相互外键触发 SAWarning
    # （测试环境 filterwarnings=["error"] 下即红）。
    for table in Base.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        have_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have_cols:
                continue
            if not col.nullable and col.server_default is None:
                # NOT NULL 且无 SQL 侧默认：已存行的表上 ADD COLUMN 必然失败，硬来会让
                # 应用起不来（比缺列更糟）。跳过并告警——这类列须人工迁移（alembic 或
                # 手工 ALTER + 回填），本函数只兜「可空/带默认」的加性变更。
                logger.warning(
                    "跳过补列 %s.%s（NOT NULL 且无 server_default，需人工迁移）",
                    table.name,
                    col.name,
                )
                continue
            ddl = CreateColumn(col).compile(dialect=conn.dialect)
            # 用 savepoint 包住：单条失败只回滚该条，不污染整个迁移事务
            # （PG 里事务内任一语句报错后，后续语句一律 InFailedSQLTransaction）。
            sp = conn.begin_nested()
            try:
                conn.execute(
                    sa.text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS {ddl}'
                    )
                )
                sp.commit()
            except sa.exc.DBAPIError as exc:
                sp.rollback()
                # 例如生成列引用了同批次里尚未补上的依赖列；跳过并告警，不让启动挂掉
                logger.warning("补列 %s.%s 失败：%s", table.name, col.name, exc)
                continue
            changed.append(f"{table.name}.{col.name}")

        have_idx = {i["name"] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in have_idx:
                continue
            ddl = str(CreateIndex(index).compile(dialect=conn.dialect))
            sp = conn.begin_nested()
            try:
                conn.execute(sa.text(ddl))
                sp.commit()
            except sa.exc.DBAPIError:
                # 多 worker 并发启动可能同时建同名索引（DuplicateTable）→ 视为已建成
                sp.rollback()
                continue
            changed.append(f"index {index.name}")
    return changed


async def _create_all() -> None:
    """create_all 降级通道：按 Base.metadata 建缺失的表，并补已存在表缺失的列/索引。

    仅在 ``settings.use_alembic=False`` 时启用。多 worker 安全：create_all 对已存在的
    表是 no-op、补列/索引均幂等，无需 Redis 迁移锁。注意必须 import 所有模型模块，
    metadata 才会被填满；模型归位后由 ``model_registry.ensure_all_models`` 统一预注册
    各模块 models.py。加性同步的边界与理由见 :func:`_sync_additive_schema`。
    """
    import sqlalchemy as sa

    from app.db.base import Base
    from app.db.model_registry import ensure_all_models
    from app.db.session import get_async_engine

    ensure_all_models()
    engine = get_async_engine()
    if engine is None:
        return
    async with engine.begin() as conn:
        # M6.9 搜索 P1：trgm 索引的 opclass 依赖 pg_trgm 扩展（索引 DDL 显式写
        # ``public.gin_trgm_ops``），故 create_all 前先幂等建扩展；否则建索引即失败。
        await conn.execute(
            sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")
        )
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_additive_schema)


async def _seed_base_data() -> None:
    """幂等写入 RBAC 默认角色→权限映射（role_permissions）。

    独立会话执行并提交；seed 用 ``ON CONFLICT DO NOTHING`` 保证并发/重复执行安全
    （见 app/modules/rbac/seed.py）。依赖 role_permissions 表已由前置 schema 初始化建出。
    """
    from app.db.session import new_session
    from app.modules.rbac.seed import seed_rbac

    db = await new_session()
    try:
        n = await seed_rbac(db)
        await db.commit()
    finally:
        await db.close()
    # n>0 仅首启/新增权限时发生；日志级即可，避免每个 worker 启动都打印噪音
    if n:
        import logging

        logging.getLogger("lkm.init_db").info("seed_rbac inserted %d rows", n)


async def init_db() -> None:
    """把数据库 schema 初始化到最新（多 worker 下用 Redis 锁串行化）。

    默认（``settings.use_alembic=False``）走 ``create_all()``：按 models metadata 建缺失表，
    开发免维护增量迁移——新增表只改 ``models.py`` 即可自动建。仅当显式设
    ``settings.use_alembic=True``（生产/历史库）才走 Alembic 增量迁移链
    （见 :func:`_create_all` 与 Alembic 各自的局限与取舍）。

    schema 就绪后恒调用 :func:`_seed_base_data` 种入 RBAC 默认权限映射，消除新部署
    需人工跑 ``python -m app.modules.rbac.seed`` 才能用后台/写端点的依赖。
    """
    from app.core.config import settings

    if not settings.use_alembic:
        await _create_all()
        await _seed_base_data()
        return
    held = await _acquire_migration_lock(_MIGRATION_LOCK_KEY)
    try:
        await asyncio.to_thread(_run_upgrade)
        await _seed_base_data()
    finally:
        await _release_migration_lock(held, _MIGRATION_LOCK_KEY)


def _run_auth_upgrade() -> None:
    """在独立线程里同步执行 auth 独立库的 Alembic upgrade head。

    与 :func:`_run_upgrade` 同因（env.py 在线迁移自带 ``asyncio.run``，须躲开
    lifespan 已运行的事件循环）：驱动 ``alembic.auth.ini`` → ``alembic_auth/``，
    其 env.py 的 URL 取自 ``settings.auth_database_url``（async→sync 方言）。
    """
    from pathlib import Path

    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(repo_root / "alembic.auth.ini"))
    command.upgrade(cfg, "head")


async def _create_auth_all() -> None:
    """auth 独立库 create_all 降级通道（``settings.use_alembic=False``）。

    只建 ``auth_metadata``（AuthBase）缺失的表，幂等；先 ``ensure_all_models()`` 把
    auth 各 models.py 注册进 metadata。连的是 auth 专属引擎（``auth_session``），
    **不碰业务库**。
    """
    from app.db.auth_base import auth_metadata
    from app.db.auth_session import get_auth_engine
    from app.db.model_registry import ensure_all_models

    ensure_all_models()
    engine = get_auth_engine()
    async with engine.begin() as conn:
        await conn.run_sync(auth_metadata.create_all)


async def init_auth_db() -> None:
    """把 **auth 独立库** schema 初始化到最新（auth 进程启动时调用）。

    与单体 :func:`init_db` 平行但分库：业务库 schema 由 backend 进程负责，auth 库由
    auth 进程自持（「进程=库边界」）。通道同 :func:`init_db`：

    - ``settings.use_alembic=False``（默认/生产 compose）→ ``auth_metadata.create_all``；
    - ``True``（历史库/显式迁移）→ 驱动 ``alembic_auth/`` 第二迁移链（锁 key 与业务链分离）。

    由 auth 进程调用而非单体：单体不实例化 auth 引擎（见 ``auth_session`` 装配规则）。
    """
    from app.core.config import settings

    if not settings.use_alembic:
        await _create_auth_all()
        logger.info("auth schema initialized via create_all (AuthBase)")
        return
    held = await _acquire_migration_lock(_AUTH_MIGRATION_LOCK_KEY)
    try:
        await asyncio.to_thread(_run_auth_upgrade)
        logger.info("auth schema upgraded via alembic_auth")
    finally:
        await _release_migration_lock(held, _AUTH_MIGRATION_LOCK_KEY)
