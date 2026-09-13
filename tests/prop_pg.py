"""属性测试专用：同步 ``@given`` 下的异步 PG schema 运行器（M5 7.2.1）。

hypothesis 的 ``@given`` 同步地反复调用测试体，而 asyncpg 连接绑定事件循环；
若每个 example 都 ``asyncio.run`` 重建 loop 复用引擎，会抛
「got Future attached to a different loop」。故本类持有**一个持久事件循环** +
单连接 StaticPool 引擎 + 独占 PG schema，所有 example 经 :meth:`run` 顺序执行。

每个 :class:`PropPG` 实例独占一个 schema（``create_all`` 指定 metadata），退出时
drop cascade；example 之间的状态清理由各测试自己在 :meth:`run` 里做。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import TracebackType
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.model_registry import ensure_all_models


class PropPG:
    """持久 loop + 独占 PG schema 的同步运行器（供 hypothesis example 复用）。"""

    def __init__(self, schema: str, *, extra_metadata: list[Any] | None = None) -> None:
        # ensure_all_models 让 Base.metadata 含全部业务表（outbox/user_dim 等）
        ensure_all_models()
        self.schema = schema
        self._metadata: list[Any] = [Base.metadata, *(extra_metadata or [])]
        self._loop = asyncio.new_event_loop()
        self.url = settings.database_url
        self.engine: AsyncEngine | None = None
        self.maker: async_sessionmaker[AsyncSession] | None = None

    def __enter__(self) -> PropPG:
        self._loop.run_until_complete(self._setup())
        return self

    async def _setup(self) -> None:
        boot = create_async_engine(self.url)
        try:
            async with boot.begin() as conn:
                await conn.execute(
                    text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
                )
                await conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        finally:
            await boot.dispose()
        # server_settings.search_path 使引擎每根连接都落该 schema（跨会话稳定，
        # 不依赖 BEGIN 内 SET——那样 rollback 会丢）。
        self.engine = create_async_engine(
            self.url,
            poolclass=StaticPool,
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        async with self.engine.begin() as conn:
            for md in self._metadata:
                await conn.run_sync(md.create_all)
        self.maker = async_sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine, expire_on_commit=False
        )

    def session(self) -> AsyncSession:
        assert self.maker is not None
        return self.maker()

    async def session_factory(self) -> AsyncSession:
        """relay_poll(session_factory=...) 兼容的异步工厂。"""
        return self.session()

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return self._loop.run_until_complete(coro)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        async def _teardown() -> None:
            assert self.engine is not None
            await self.engine.dispose()
            drop = create_async_engine(self.url, poolclass=NullPool)
            try:
                async with drop.begin() as conn:
                    await conn.execute(
                        text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
                    )
            finally:
                await drop.dispose()

        try:
            self._loop.run_until_complete(_teardown())
        finally:
            self._loop.close()
