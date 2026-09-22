"""Alembic environment configuration for online/offline migrations."""

import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import settings

# Alembic Config object
config = context.config

# Configure logging from the ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set the database URL
# configparser 把 % 当插值起始符（ini 里的 %(here)s 靠它）。settings 的连接串用 quote
# 编码密码，密码含保留字符时会出现裸 %XX，set_main_option 会当场抛
# ValueError(invalid interpolation syntax) 让迁移根本起不来（实测）。按 configparser
# 规则转义成 %%，get_main_option/get_section 读回时还原成原值。
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# Import all models so metadata is fully populated for autogenerate
from app.db.base import Base
from app.db.model_registry import ensure_all_models

ensure_all_models()

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' (SQL-script) mode.

    Configures the context with just a URL, not an Engine.
    Calls to ``context.execute()`` emit the given SQL to the script output.
    """
    # 与 online 路径同一口径：离线生成 SQL 也要走同步方言，否则读的是 import 时写入的
    # asyncpg URL，产物按 asyncpg 方言渲染、且要额外依赖 async 驱动可导入
    url = _sync_url(config.get_main_option("sqlalchemy.url") or "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _sync_url(url: str) -> str:
    """Alembic 在同步上下文跑——把 asyncpg 驱动换成同步 psycopg2。

    settings.database_url 是 ``postgresql+asyncpg``；alembic(postgresql://)*不可直接用，
    回落 psycopg2（装同步驱动）。
    """
    if url.startswith("postgresql+asyncpg"):
        return "postgresql+psycopg2" + url[len("postgresql+asyncpg") :]
    return url


def run_migrations_online() -> None:
    """Run migrations in 'online' (live-database) mode.

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
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
