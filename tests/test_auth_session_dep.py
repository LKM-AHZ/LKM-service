"""``app.db.auth_session`` 请求级会话依赖的契约测试（防回潮）。

2026-09-18 真机定位的缺陷：``get_auth_session`` 曾是**普通协程依赖**（``return session``），
没有任何人提交该会话——而 auth 域的 service 层普遍只 ``flush`` 并假定「外层会话会提交」
（如 ``service_auth.register_local`` 的注释）。结果是所有 auth 写操作在请求结束时被回滚：
``/auth/reg/local`` 返回 200 且给出 ``user_id``，但 ``auth.users`` 始终 0 行。

单测套件此前掩盖了它：``auth_front_client`` / ``auth_app_client`` 把该依赖 override 成
``yield auth_db``（共享长活会话、不提交），测试体内直查同一会话故「看得见」写入。
故本文件的断言**不经 override**，直接对依赖生成器本身验契约。
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from app.db import auth_session as mod


class _FakeSession:
    """记录 commit/rollback/close 调用顺序的假会话。"""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def commit(self) -> None:
        self._calls.append("commit")

    async def rollback(self) -> None:
        self._calls.append("rollback")

    async def close(self) -> None:
        self._calls.append("close")


@pytest.fixture
def fake_factory() -> Iterator[tuple[Any, list[str]]]:
    calls: list[str] = []

    def _factory() -> _FakeSession:
        return _FakeSession(calls)

    class _Maker:
        def __call__(self) -> _FakeSession:
            return _factory()

    with patch.object(mod, "_get_auth_session_local", lambda: _Maker()):
        yield _Maker, calls


async def test_get_auth_session_commits_on_success(
    fake_factory: tuple[Any, list[str]],
) -> None:
    """成功路径：退出依赖时必须 commit，然后 close（缺 commit 即本轮真机缺陷）。"""
    _, calls = fake_factory
    agen = mod.get_auth_session()
    session = await agen.__anext__()
    assert isinstance(session, _FakeSession)

    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    assert calls == ["commit", "close"]


async def test_get_auth_session_rolls_back_on_error(
    fake_factory: tuple[Any, list[str]],
) -> None:
    """异常路径：rollback 后 close，不得提交半截写入。"""
    _, calls = fake_factory
    agen = mod.get_auth_session()
    await agen.__anext__()

    with pytest.raises(RuntimeError, match="boom"):
        await agen.athrow(RuntimeError("boom"))

    assert calls == ["rollback", "close"]


async def test_new_auth_session_returns_session_without_lifecycle(
    fake_factory: tuple[Any, list[str]],
) -> None:
    """内部调用方（后台巡检/导出/双库同步）用 new_auth_session：自行 commit/close。"""
    _, calls = fake_factory
    session = await mod.new_auth_session()
    assert isinstance(session, _FakeSession)
    assert calls == []
