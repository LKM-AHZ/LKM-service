"""数据库初始化 —— Alembic 为 schema 唯一权威。

多 worker安全：每个 uvicorn worker 的 lifespan 都会调 init_db()。
首次建库时并发 upgrade 会有竞态（重复建表/版本锁冲突），故用 Redis 分布式锁串行化；
Redis 不可用（未配置/宕机，fail-open）则不设锁直接跑（dev 单 worker 本无并发）。

**auth 独立库**的 schema 初始化（``init_auth_db``）按「进程=库边界」由 auth 进程自持，
已归 ``auth.db.init``；本模块只负责业务库，不触达 auth 库。
两条迁移链的锁按库分 key，通用实现在 ``app.db.migration_lock``。
"""

import asyncio
import logging
from typing import Any

from app.db.migration_lock import (
    acquire_migration_lock,
    release_migration_lock,
)
from app.db.shared_objects import ensure_shared_objects

logger = logging.getLogger("lkm.init_db")

_MIGRATION_LOCK_KEY = "lkm:migration:lock"


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


# ---- TimescaleDB 装配（批 2，路线图 §8 #40）----
#
# `outbox_events` / `outbox_archived` 转为 **hypertable**：按 ``created_at`` 自动时间分区
# （chunk 裁剪让 relay 的领取查询只扫近期 chunk）、冷历史列式压缩、保留策略兜底。
#
# 与 ``outbox_archived`` 冷表归档的**分工**（两者语义重叠，必须分明）：
#   - hypertable 管**分区与压缩**——chunk 时间裁剪、列式压缩、超期 chunk 的 DROP；
#   - ``outbox_relay.archive_published()`` 管**可查历史**——已投递行按应用侧保留期
#     （``outbox_archive_retention_s``，默认 7 天）迁进 ``outbox_archived`` 再删。
# 正常路径下 outbox_events 的超期行由应用侧搬走，因此 Timescale 的保留策略只是
# **兜底**（应用侧停摆/异常滞留时防表无限膨胀），阈值刻意远大于归档保留期。
#
# 装配是**可选增强**：非 Timescale 镜像（含测试用的 postgres:16-alpine）或未预加载
# ``timescaledb`` 的实例上扩展建不起来，此时告警并整体跳过——表退化为普通表，
# 主键里多一列 ``created_at`` 无副作用，链路的 DML 语义完全不变。
#
# **outbox_events 刻意不启用压缩**：本表有热更新（relay 反复 UPDATE
# ``status``/``attempt_count``/``locked_at``/``published_at``），而压缩 chunk 默认不可
# DML——一旦有滞留行被压进只读 chunk，relay 将永久投不出它。本表活跃窗口 ≤ 归档保留
# 期（7 天）、体量极小，压缩收益可忽略；压缩的收益集中在只增不更的 ``outbox_archived``。
_TIMESCALE_CHUNK_INTERVAL = "7 days"

# (表名, 分区列, chunk 间隔)
_HYPERTABLE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("outbox_events", "created_at", _TIMESCALE_CHUNK_INTERVAL),
    ("outbox_archived", "created_at", _TIMESCALE_CHUNK_INTERVAL),
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
        # 多进程并发首启（compose 下 backend+auth+9 worker 同时拉起）可能撞 DuplicateObject：
        # 扩展其实已由别的进程建好，复查一次扩展目录，避免把它误判成「引擎不可用」而
        # 整个进程跳过 hypertable 装配。
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
    建表后另做 TimescaleDB 装配（hypertable + 压缩/保留策略），见 :func:`_ensure_hypertables`。
    """
    from app.db.base import Base
    from app.db.model_registry import ensure_all_models
    from app.db.session import get_async_engine

    ensure_all_models()
    engine = get_async_engine()
    if engine is None:
        return
    async with engine.begin() as conn:
        await ensure_shared_objects(conn)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_additive_schema)
        # 建表之后：hypertable 转换只对已存在的表有意义；扩展不可用时静默跳过（见上）。
        if await _ensure_timescaledb(conn):
            await _ensure_hypertables(conn)


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
        logger.info("seed_rbac inserted %d rows", n)


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
        # 建表同样要上锁（理由见 _create_all docstring）；Redis 不可用时锁 fail-open，
        # 退回「不设锁直接建」的原语义
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
        await _seed_base_data()
    finally:
        await release_migration_lock(held, _MIGRATION_LOCK_KEY)


