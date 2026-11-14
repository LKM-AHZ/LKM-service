"""Alembic environment configuration for online/offline migrations."""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import engine_from_config, pool

from alembic import context
from core.config import settings

# Alembic Config object
config = context.config

# Configure logging from the ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 按 configparser 规则转义连接串中的百分号。
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# Import all models so metadata is fully populated for autogenerate
from app import bootstrap as app_bootstrap
from app.modules.spec import MIGRATION_DIRS
from core.db.base import Base
from core.db.model_registry import ensure_all_models
from core.db.shared_objects import TIMESCALE_AUTO_INDEXES

app_bootstrap.register()
ensure_all_models()

# 插件自带迁移目录：与宿主 versions 一起纳入 Alembic 的 revision 搜索路径。
# ``alembic.ini`` 的 path_separator=os 决定分隔符（os.pathsep）；显式写死以免回落到
# 「按空格/逗号切分」的旧规则，让含空格或分隔符的插件路径错位。
if MIGRATION_DIRS:
    host_versions = Path(__file__).resolve().parent / "versions"
    config.set_main_option(
        "version_locations",
        os.pathsep.join(str(path) for path in (host_versions, *MIGRATION_DIRS)),
    )

target_metadata = Base.metadata


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """autogenerate 反射过滤：排除 TimescaleDB 自动建的 hypertable 索引。

    这些 ``<表>_<时间列>_idx`` 由扩展在装配 hypertable 时创建、模型不声明，
    不过滤则每次 autogenerate/``alembic check`` 都误报 ``remove_index``。
    """
    if reflected and type_ == "index" and name in TIMESCALE_AUTO_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' (SQL-script) mode.

    Configures the context with just a URL, not an Engine.
    Calls to ``context.execute()`` emit the given SQL to the script output.
    """
    url = _sync_url(config.get_main_option("sqlalchemy.url") or "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _sync_url(url: str) -> str:
    """
    Alembic 在同步上下文跑——把 asyncpg 驱动换成同步 psycopg2。
    settings.database_url 是 ``postgresql+asyncpg``；alembic(postgresql://)*不可直接用，
    回落 psycopg2（装同步驱动）。
    """
    if url.startswith("postgresql+asyncpg"):
        return "postgresql+psycopg2" + url[len("postgresql+asyncpg") :]
    return url


def run_migrations_online() -> None:
    """
    Run migrations in 'online' (live-database) mode.
    Creates an Engine and associates a connection with the context.
    统一 PostgreSQL 目标。
    """
    config.set_main_option(
        "sqlalchemy.url", _sync_url(settings.database_url).replace("%", "%%")
    )
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
