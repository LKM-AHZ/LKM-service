"""
数据库初始化 —— Alembic 为 schema 唯一权威。
多 worker安全：每个 uvicorn worker 的 lifespan 都会调 init_db()。
首次建库时并发 upgrade 会有竞态（重复建表/版本锁冲突），故用 Redis 分布式锁串行化；
Redis 不可用（未配置/宕机，fail-open）则不设锁直接跑（dev 单 worker 本无并发）。
**auth 独立库**的 schema 初始化（``init_auth_db``）按「进程=库边界」由 auth 进程自持，
已归 ``auth.db.init``；本模块只负责业务库，不触达 auth 库。
两条迁移链的锁按库分 key，通用实现在 ``core.db.migration_lock``。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from core.db.migration_lock import (
    acquire_migration_lock,
    release_migration_lock,
)
from core.db.shared_objects import ensure_shared_objects

logger = logging.getLogger("lkm.init_db")

_MIGRATION_LOCK_KEY = "lkm:migration:lock"

_ONLINE_DDL_TABLES = frozenset(
    {
        "content_items",
        "content_comments",
        "outbox_events",
        "outbox_archived",
        "interaction_view_logs",
        "points_ledger",
    }
)
_ONLINE_DDL_MIN_BYTES = 64 * 1024 * 1024

#: schema 就绪后需执行的幂等 seed 步骤（``async (db) -> int``，返回插入行数）。
#: 由各顶层包 bootstrap 登记（如 ``app.bootstrap`` 登记 seed_rbac）——core 因而不知道
#: 任何业务 seed 模块名。
_SEED_STEPS: list[Callable[[Any], Awaitable[int]]] = []


def register_seed_step(fn: Callable[[Any], Awaitable[int]]) -> None:
    """登记一个 seed 步骤（幂等；重复登记只保留一份）。"""
    if fn not in _SEED_STEPS:
        _SEED_STEPS.append(fn)


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

    from core.db.base import Base

    changed: list[str] = []
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # 按登记顺序检查表的新增列和索引。
    for table in Base.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        large_existing = False
        if table.name in _ONLINE_DDL_TABLES:
            relation_bytes = conn.execute(
                sa.text("SELECT pg_total_relation_size(to_regclass(:name))"),
                {"name": table.name},
            ).scalar_one()
            large_existing = (relation_bytes or 0) >= _ONLINE_DDL_MIN_BYTES
            # hypertable 根表的 pg_total_relation_size 不含各 chunk，可能只有几 KB。
            # 一旦转成 hypertable，就始终走在线 DDL，不以根表体量作判断。
            if not large_existing and table.name in {
                "outbox_events",
                "outbox_archived",
                "points_ledger",
            }:
                has_timescale_view = conn.execute(
                    sa.text("SELECT to_regclass('timescaledb_information.hypertables')")
                ).scalar_one()
                if has_timescale_view is not None:
                    large_existing = bool(
                        conn.execute(
                            sa.text(
                                "SELECT EXISTS (SELECT 1 FROM "
                                "timescaledb_information.hypertables "
                                "WHERE hypertable_schema = current_schema() "
                                "AND hypertable_name = :name)"
                            ),
                            {"name": table.name},
                        ).scalar_one()
                    )
        have_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have_cols:
                continue
            if large_existing:
                raise RuntimeError(
                    f"大表 {table.name} 缺少列 {col.name}；请先用 "
                    "scripts/online_ddl.py 分阶段迁移，再发布新代码"
                )
            if not col.nullable and col.server_default is None:
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
            if large_existing:
                logger.warning(
                    "大表 %s 缺少索引 %s；请用 scripts/online_ddl.py index "
                    "并发创建，启动事务不会自动建索引",
                    table.name,
                    index.name,
                )
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


_TIMESCALE_CHUNK_INTERVAL = "7 days"

# (表名, 分区列, chunk 间隔)
_HYPERTABLE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("outbox_events", "created_at", _TIMESCALE_CHUNK_INTERVAL),
    ("outbox_archived", "created_at", _TIMESCALE_CHUNK_INTERVAL),
    # 积分流水转换为 hypertable，保持未压缩。
    ("points_ledger", "created_at", _TIMESCALE_CHUNK_INTERVAL),
)

# 启用列式压缩的表：(表名, compress_segmentby, compress_orderby)
_COMPRESSION_SPECS: tuple[tuple[str, str, str], ...] = (
    ("outbox_archived", "routing_key", "created_at DESC"),
)

# 压缩策略：(表名, 阈值)——超期 chunk 转列式压缩
_COMPRESSION_POLICIES: tuple[tuple[str, str], ...] = (("outbox_archived", "7 days"),)

# 保留策略：(表名, 阈值)——超期 chunk 直接 DROP（仅 outbox_events，兜底；
# outbox_archived 是「可查历史」，刻意不设，其增长由归档链路约束）
_RETENTION_POLICIES: tuple[tuple[str, str], ...] = (("outbox_events", "30 days"),)

_CONTINUOUS_AGGREGATE_SPECS: tuple[tuple[str, str], ...] = (
    (
        "points_daily",
        """
        SELECT time_bucket(INTERVAL '1 day', created_at) AS bucket,
               reason,
               sum(delta) AS delta_sum,
               count(*)   AS entry_count
        FROM points_ledger
        WHERE delta > 0
        GROUP BY bucket, reason
        """,
    ),
)

# (视图名, start_offset, end_offset, schedule_interval)
_CONTINUOUS_AGGREGATE_POLICIES: tuple[tuple[str, str, str, str], ...] = (
    ("points_daily", "30 days", "1 hour", "1 hour"),
)


async def _ensure_timescaledb(conn: Any) -> bool:
    """建 ``timescaledb`` 扩展（幂等）；返回扩展是否可用。

    不可用（普通 PG 镜像、未预加载 ``timescaledb``）时告警并返回 False，调用方据此
    跳过 hypertable 装配而不影响启动。用 savepoint 包裹：PG 事务内任一语句报错后
    后续语句一律 ``InFailedSQLTransaction``，不隔离会把整个初始化带崩。
    """
    import sqlalchemy as sa

    sp = await conn.begin_nested()
    try:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        await sp.commit()
        return True
    except sa.exc.DBAPIError as exc:
        await sp.rollback()
        exists = await conn.scalar(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        )
        if exists:
            return True
        logger.warning(
            "timescaledb 扩展不可用（非 Timescale 镜像或未预加载），跳过 hypertable 装配：%s",
            exc,
        )
        return False


async def _ensure_hypertables(conn: Any) -> list[str]:
    """把 outbox 两表转 hypertable 并装配压缩/保留策略（幂等；须先 :func:`_ensure_timescaledb`）。

    逐条 DDL 独立 savepoint：Timescale 的策略函数不支持 ``IF NOT EXISTS`` 的地方
    （如 ``ALTER TABLE ... SET (timescaledb.compress)``）重复执行会报错，视为「已装配」
    跳过即可，不能让单条失败污染整批。返回实际生效的装配项，供启动日志与测试断言。
    """
    import sqlalchemy as sa

    changed: list[str] = []

    async def _run(sql: str, label: str) -> None:
        sp = await conn.begin_nested()
        try:
            await conn.execute(sa.text(sql))
            await sp.commit()
        except sa.exc.DBAPIError as exc:
            await sp.rollback()
            logger.warning("TimescaleDB %s 失败（按已装配跳过）：%s", label, exc)
            return
        changed.append(label)

    for table, column, interval in _HYPERTABLE_SPECS:
        await _run(
            f"SELECT create_hypertable('{table}', '{column}', "
            f"chunk_time_interval => INTERVAL '{interval}', "
            f"if_not_exists => TRUE, migrate_data => TRUE)",
            f"hypertable:{table}",
        )
    for table, segmentby, orderby in _COMPRESSION_SPECS:
        await _run(
            f"ALTER TABLE {table} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segmentby}', "
            f"timescaledb.compress_orderby = '{orderby}')",
            f"compress:{table}",
        )
    for table, threshold in _COMPRESSION_POLICIES:
        await _run(
            f"SELECT add_compression_policy('{table}', INTERVAL '{threshold}', "
            f"if_not_exists => TRUE)",
            f"compression_policy:{table}",
        )
    for table, threshold in _RETENTION_POLICIES:
        await _run(
            f"SELECT add_retention_policy('{table}', INTERVAL '{threshold}', "
            f"if_not_exists => TRUE)",
            f"retention_policy:{table}",
        )
    return changed


async def _ensure_continuous_aggregates(engine: Any) -> list[str]:
    """在 hypertable 上装配 continuous aggregate（幂等；须先 :func:`_ensure_hypertables`）。

    Args:
        engine: 业务库 ``AsyncEngine``。本函数**自开 AUTOCOMMIT 连接**，刻意不复用调用方的
            事务（理由见下）。

    **必须在事务块之外**：TimescaleDB 的 ``CREATE MATERIALIZED VIEW ... WITH
    (timescaledb.continuous)``（隐含 ``WITH DATA``）**不允许在事务块内运行**——放进
    ``engine.begin()`` 只会拿到 ``cannot run inside a transaction block``，而本模块的容错会把它
    当成「已装配」静默吞掉，现场表现为「扩展可用、hypertable 也转了，但 cagg 始终不存在」。
    故这里用 ``isolation_level="AUTOCOMMIT"`` 的独立连接：每条语句自成事务，失败天然不污染
    后续，try/except 记告警即可（也因此**不能**沿用 :func:`_ensure_hypertables` 的
    ``begin_nested()`` 逐条 savepoint 骨架）。

    幂等：``CREATE ... continuous`` **没有** ``IF NOT EXISTS``，先查
    ``timescaledb_information.continuous_aggregates`` 判存在；刷新策略的
    ``add_continuous_aggregate_policy`` 自带 ``if_not_exists``。

    **刻意不写 ``timescaledb.materialized_only``**：该参数名随 TimescaleDB 版本变动，写死会让
    旧/新版本上的 CREATE 直接失败。接受实例默认——纯物化则按策略刷新（报表面容忍滞后），
    real-time 默认则查询时自动合并未物化部分。报表读口因此**不假设新鲜度**。
    """
    import sqlalchemy as sa

    changed: list[str] = []

    async def _run(conn: Any, sql: str, label: str) -> None:
        try:
            await conn.execute(sa.text(sql))
        except sa.exc.DBAPIError as exc:
            logger.warning("TimescaleDB %s 失败（按已装配跳过）：%s", label, exc)
            return
        changed.append(label)

    auto = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with auto.connect() as conn:
        try:
            has_timescale = await conn.scalar(
                sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            )
        except sa.exc.DBAPIError as exc:
            logger.warning("TimescaleDB 扩展探测失败，跳过 continuous aggregate：%s", exc)
            return []
        if has_timescale is None:
            return []

        existing: set[str] = set()
        try:
            rows = await conn.execute(
                sa.text(
                    "SELECT view_name"
                    " FROM timescaledb_information.continuous_aggregates"
                )
            )
            existing = {row[0] for row in rows}
        except sa.exc.DBAPIError as exc:
            logger.warning("TimescaleDB cagg 目录查询失败，按无已装配视图处理：%s", exc)

        for name, definition in _CONTINUOUS_AGGREGATE_SPECS:
            if name in existing:
                continue
            await _run(
                conn,
                f"CREATE MATERIALIZED VIEW {name} "
                f"WITH (timescaledb.continuous) AS {definition}",
                f"cagg:{name}",
            )
        for name, start_offset, end_offset, schedule in _CONTINUOUS_AGGREGATE_POLICIES:
            await _run(
                conn,
                f"SELECT add_continuous_aggregate_policy('{name}', "
                f"start_offset => INTERVAL '{start_offset}', "
                f"end_offset => INTERVAL '{end_offset}', "
                f"schedule_interval => INTERVAL '{schedule}', "
                f"if_not_exists => TRUE)",
                f"cagg_policy:{name}",
            )
    return changed


async def _create_all() -> None:
    """create_all 降级通道：按 Base.metadata 建缺失的表，并补已存在表缺失的列/索引。

    仅在 ``settings.use_alembic=False`` 时启用。调用方（:func:`init_db`）会用 Redis
    迁移锁串行化本函数：``create_all`` 的 ``checkfirst`` 是**先查后建**的非原子检查，
    而 compose 默认同时拉起 backend + 多个 worker，首次建库时两个进程可能同时判定某表
    缺失并发起 ``CREATE TABLE``，后到者拿到 ``DuplicateTable``/``ProgrammingError``
    直接启动失败（补列/补索引与 hypertable 装配都已容错，唯独建表这一步不能裸奔）。
    注意必须 import 所有模型模块，
    metadata 才会被填满；模型归位后由 ``model_registry.ensure_all_models`` 统一预注册
    各模块 models.py。加性同步的边界与理由见 :func:`_sync_additive_schema`；
    建表后另做 TimescaleDB 装配（hypertable + 压缩/保留策略 + continuous aggregate），
    见 :func:`_ensure_hypertables` 与 :func:`_ensure_continuous_aggregates`。
    """
    from core.db.base import Base
    from core.db.model_registry import ensure_all_models
    from core.db.session import get_async_engine

    ensure_all_models()
    engine = get_async_engine()
    if engine is None:
        return
    changed: list[str] = []
    timescale_ready = False
    async with engine.begin() as conn:
        await ensure_shared_objects(conn)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_additive_schema)
        if await _ensure_timescaledb(conn):
            changed = await _ensure_hypertables(conn)
            timescale_ready = True
    if timescale_ready:
        changed += await _ensure_continuous_aggregates(engine)
    if changed:
        logger.info("TimescaleDB 装配：%s", ", ".join(changed))


async def _ensure_caggs_after_migration() -> None:
    """Alembic 通道下补装配 continuous aggregate（迁移里建不了，见 0004 的模块 docstring）。

    ``CREATE MATERIALIZED VIEW ... (timescaledb.continuous)`` 在事务块内一律失败（连 DO 块的
    隐式事务都过不去），故迁移 0004 只把 ``points_ledger`` 转成 hypertable，cagg 挪到这里——
    与 create_all 通道共用 :func:`_ensure_continuous_aggregates`（自开 AUTOCOMMIT、幂等）。
    """
    from core.db.session import get_async_engine

    engine = get_async_engine()
    if engine is None:
        return
    changed = await _ensure_continuous_aggregates(engine)
    if changed:
        logger.info("TimescaleDB 装配（迁移通道）：%s", ", ".join(changed))


async def _seed_base_data() -> None:
    """幂等执行全部已登记的 seed 步骤（如 RBAC 默认角色→权限映射）。

    独立会话执行并提交；各步骤自身用 ``ON CONFLICT DO NOTHING`` 保证并发/重复执行安全
    （如 app/modules/rbac/seed.py）。依赖相关表已由前置 schema 初始化建出。
    """
    if not _SEED_STEPS:
        return
    from core.db.session import new_worker_session as new_session

    db = await new_session()
    try:
        total = 0
        for step in _SEED_STEPS:
            total += await step(db)
        await db.commit()
    finally:
        await db.close()
    if total:
        logger.info("seed steps inserted %d rows", total)


_db_initialized: bool = False


def is_db_initialized() -> bool:
    """本进程的业务库 schema 是否已初始化成功（失败或仍在进行中均为 False）。"""
    return _db_initialized


async def init_db() -> None:
    """把数据库 schema 初始化到最新（多 worker 下用 Redis 锁串行化）。

    默认（``settings.use_alembic=False``）走 ``create_all()``：按 models metadata 建缺失表，
    开发免维护增量迁移——新增表只改 ``models.py`` 即可自动建。仅当显式设
    ``settings.use_alembic=True``（生产/历史库）才走 Alembic 增量迁移链
    （见 :func:`_create_all` 与 Alembic 各自的局限与取舍）。

    schema 就绪后恒调用 :func:`_seed_base_data` 种入 RBAC 默认权限映射，消除新部署
    需人工跑 ``python -m app.modules.rbac.seed`` 才能用后台/写端点的依赖。

    成败记入 ``_db_initialized``：失败必须显式回落为 False，否则「DB 可达但没建好表」会被
    readiness 当成就绪。
    """
    global _db_initialized
    try:
        await _init_db_schema()
    except Exception:
        _db_initialized = False
        raise
    _db_initialized = True


async def _init_db_schema() -> None:
    """:func:`init_db` 的实际建表/迁移体；成败由 init_db 记录进 ``_db_initialized``。"""
    from core.config import settings

    if not settings.use_alembic:
        held = await acquire_migration_lock(_MIGRATION_LOCK_KEY)
        try:
            await _create_all()
        finally:
            await release_migration_lock(held, _MIGRATION_LOCK_KEY)
        await _seed_base_data()
        return
    held = await acquire_migration_lock(_MIGRATION_LOCK_KEY)
    try:
        await asyncio.to_thread(_run_upgrade)
        # cagg 在迁移事务内建不了（0004 docstring），迁移跑完后在此补装配
        await _ensure_caggs_after_migration()
        await _seed_base_data()
    finally:
        await release_migration_lock(held, _MIGRATION_LOCK_KEY)
