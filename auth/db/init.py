"""AUTH 独立库 schema 初始化（「进程=库边界」：由 auth 进程自持）。

原位于 ``app/db/init_db.py``，随 auth 拆包迁入：auth 库的 create_all 降级通道与
``alembic_auth`` 第二迁移链只服务 auth 进程，归 auth 包持有；业务库链仍由
``app.db.init_db`` 负责，两者互不触达。

通道与业务库一致：

- ``settings.use_alembic=False``（默认/生产 compose）→ ``auth_metadata.create_all``；
- ``True``（历史库/显式迁移）→ 驱动 ``alembic_auth/``（锁 key 与业务链分离，
  见 ``app.db.migration_lock``，避免业务/auth 迁移互相阻塞）。

由 auth 进程调用而非单体：单体不实例化 auth 引擎（见 ``auth.db.session`` 装配规则）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.core.config import settings
from app.db.migration_lock import acquire_migration_lock, release_migration_lock
from app.db.shared_objects import ensure_shared_objects

logger = logging.getLogger("lkm.init_db")

_AUTH_MIGRATION_LOCK_KEY = "lkm:migration:auth:lock"


def _run_auth_upgrade() -> None:
    """在独立线程里同步执行 auth 独立库的 Alembic upgrade head。

    与 ``app.db.init_db._run_upgrade`` 同因（env.py 在线迁移自带 ``asyncio.run``，须躲开
    lifespan 已运行的事件循环）：驱动 ``alembic.auth.ini`` → ``alembic_auth/``，
    其 env.py 的 URL 取自 ``settings.auth_database_url``（async→sync 方言）。
    """
    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(repo_root / "alembic.auth.ini"))
    command.upgrade(cfg, "head")


async def _create_auth_all() -> None:
    """auth 独立库 create_all 降级通道（``settings.use_alembic=False``）。

    只建 ``auth_metadata``（AuthBase）缺失的表，幂等；先 ``register_models()`` 把
    auth 各 models.py 注册进 metadata。连的是 auth 专属引擎（``auth.db.session``），
    **不碰业务库**。
    """
    from auth import register_models
    from auth.db.base import auth_metadata
    from auth.db.session import get_auth_engine

    register_models()
    engine = get_auth_engine()
    async with engine.begin() as conn:
        # auth 库是**独立 database**，其 public schema 与业务库互不相通，
        # uuid7 函数须各自建一份（表的 id 列 server_default 指向 public.uuid_generate_v7()）。
        await ensure_shared_objects(conn)
        await conn.run_sync(auth_metadata.create_all)


async def init_auth_db() -> None:
    """把 **auth 独立库** schema 初始化到最新（auth 进程启动时调用）。

    业务库 schema 由 backend 进程负责，auth 库由 auth 进程自持（「进程=库边界」）。
    """
    if not settings.use_alembic:
        await _create_auth_all()
        logger.info("auth schema initialized via create_all (AuthBase)")
        return
    held = await acquire_migration_lock(_AUTH_MIGRATION_LOCK_KEY)
    try:
        await asyncio.to_thread(_run_auth_upgrade)
        logger.info("auth schema upgraded via alembic_auth")
    finally:
        await release_migration_lock(held, _AUTH_MIGRATION_LOCK_KEY)
