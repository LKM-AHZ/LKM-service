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
import contextlib
import os
import re
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

#: schema 名要拼进 DDL，必须是裸标识符（见 PropPG.__init__ 的校验）
_SCHEMA_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PropPG:
    """持久 loop + 独占 PG schema 的同步运行器（供 hypothesis example 复用）。"""

    def __init__(self, schema: str, *, extra_metadata: list[Any] | None = None) -> None:
        # ensure_all_models 让 Base.metadata 含全部业务表（outbox/user_dim 等）
        ensure_all_models()
        # schema 名含 pid：xdist 并行时各 worker 的独立进程不撞名。
        # 这个名字会被直接拼进 CREATE/DROP SCHEMA ... CASCADE 的 DDL，故必须是裸标识符：
        # 带引号/分号的名字会拼出畸形 SQL，最坏会在 DROP ... CASCADE 时删掉非预期对象。
        self.schema = f"{schema}_{os.getpid()}"
        if not _SCHEMA_RE.fullmatch(self.schema):
            raise ValueError(
                f"非法 schema 名 {self.schema!r}：只允许 [A-Za-z_][A-Za-z0-9_]*"
            )
        self._metadata: list[Any] = [Base.metadata, *(extra_metadata or [])]
        self._loop = asyncio.new_event_loop()
        self.url = settings.database_url
        self.engine: AsyncEngine | None = None
        self.maker: async_sessionmaker[AsyncSession] | None = None

    def __enter__(self) -> PropPG:
        try:
            self._loop.run_until_complete(self._setup())
        except BaseException:
            # _setup 中途失败（建 schema / create_all / 建引擎）时 __exit__ 不会被调用，
            # 这里必须自己收尾：否则该 PG schema 与事件循环会一直泄漏到进程结束。
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._teardown())
            self._loop.close()
            raise
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
        # 显式 raise 而非 assert：-O 下 assert 会被剥掉，之后错用会在别处报
        # "NoneType object is not callable"，掩盖真正原因（setup 没跑/已 teardown）。
        if self.maker is None:
            raise RuntimeError("PropPG 尚未 setup（或已 teardown），无法取会话")
        return self.maker()

    async def session_factory(self) -> AsyncSession:
        """relay_poll(session_factory=...) 兼容的异步工厂。"""
        return self.session()

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return self._loop.run_until_complete(coro)

    async def _teardown(self) -> None:
        """取消残留任务 → 释放引擎 → drop 该 schema。抽成方法而非 __exit__ 内嵌函数：
        __enter__ 失败路径也要用它（此时 engine 可能还没建出来，故不能用 assert）。"""
        # 先取消本 loop 上尚未完成的 task（如 relay_poll 的后台轮询）：loop.close() 不会
        # 回收它们，之后会报 "Task was destroyed but it is pending!" /
        # "Event loop is closed"；排除当前 task（就是本协程自己）。
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.engine is not None:
            await self.engine.dispose()
        drop = create_async_engine(self.url, poolclass=NullPool)
        try:
            async with drop.begin() as conn:
                await conn.execute(
                    text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
                )
        finally:
            await drop.dispose()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._loop.run_until_complete(self._teardown())
        finally:
            self._loop.close()
