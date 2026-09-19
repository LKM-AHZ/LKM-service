"""AUTH 独立库 Alembic environment（M3.B S1 第二迁移链，online/offline 通用）。

独立 database 承载 auth 自持表；只针对 ``AuthBase``/``auth_metadata``（app/db/auth_base.py）。
S1–S5 auth.models 仍挂在 monolith Base 上、auth_metadata 为空，此链仅空跑占位；
S5 把 auth.models 迁到 AuthBase 后，本链经 autogenerate 产身具 auth 库迁移。
``alembic -c alembic.auth.ini`` 驱动时 URL 取自 ``settings.auth_database_url``（async→sync）。
"""

import sys
from logging.config import fileConfig
from pathlib import Path

# 让 alembic 能找到 app / auth 包（从仓库根 sys.path 挂载）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import settings

# Alembic Config object
config = context.config

# 配置日志（若本 ini/被驱动配置有 fileConfig 段）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata = auth 独立库（AuthBase）
from auth.db.base import auth_metadata

target_metadata = auth_metadata


def _sync_url(url: str) -> str:
    """Alembic 运行于同步上下文——把 asyncpg 换成同步 psycopg2（同主链 env.py 策略）。"""
    if url.startswith("postgresql+asyncpg"):
        return "postgresql+psycopg2" + url[len("postgresql+asyncpg") :]
    return url


def _auth_url() -> str:
    # 统一走 settings.auth_database_url（本 ini 的 sqlalchemy.url 仅是占位）
    return settings.auth_database_url


def run_migrations_offline() -> None:
    url = _sync_url(_auth_url())
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _sync_url(_auth_url()))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
