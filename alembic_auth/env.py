"""
AUTH 独立库 Alembic environment（M3.B S1 第二迁移链，online/offline 通用）。
"""

import sys
from logging.config import fileConfig
from pathlib import Path

# 让 alembic 能找到 app / auth 包（从仓库根 sys.path 挂载）。
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from sqlalchemy import engine_from_config, pool

from alembic import context
from core.config import settings

# Alembic Config object
config = context.config

# 配置日志（若本 ini/被驱动配置有 fileConfig 段）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata = auth 独立库（AuthBase）。auth 模型不挂在业务 Base 上，
# 由 auth 自己的注册钩子导入并 configure（幂等）。
from auth import register as auth_register
from auth.db.base import auth_metadata

auth_register.register_models()

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
    config.set_main_option("sqlalchemy.url", _sync_url(_auth_url()).replace("%", "%%"))
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
