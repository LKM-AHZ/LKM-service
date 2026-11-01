"""L2 命令总线骨架的契约测试。

骨架阶段没有生产调用方，所以这里把「分发语义」逐条钉死：注册幂等、重名拒收、
未注册 fail-fast、请求/响应类型校验、以及**同一个 session 被透传**（同事务的唯一保证）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.bus import dispatcher


@dataclass(frozen=True)
class _EchoReq:
    text: str


@dataclass(frozen=True)
class _EchoResp:
    echoed: str


_echo = dispatcher.Command("test.echo", _EchoReq, _EchoResp)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    dispatcher.reset()
    yield
    dispatcher.reset()


async def test_call_dispatches_to_registered_handler() -> None:
    @dispatcher.register(_echo)
    async def _handle(req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
        return _EchoResp(echoed=req.text)

    assert dispatcher.is_registered(_echo)
    assert await dispatcher.call(_echo, _EchoReq("hi"), db=object()) == _EchoResp("hi")


async def test_call_passes_the_caller_session_through() -> None:
    """同事务的关键：handler 收到的必须是调用方那个 session（不是新开的）。"""
    seen: list[object] = []

    @dispatcher.register(_echo)
    async def _handle(_req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
        seen.append(db)
        return _EchoResp(echoed="ok")

    session = object()
    await dispatcher.call(_echo, _EchoReq("x"), db=session)
    assert seen == [session]


async def test_unregistered_command_fails_fast() -> None:
    with pytest.raises(dispatcher.CommandNotRegistered, match="未注册"):
        await dispatcher.call(_echo, _EchoReq("x"), db=object())


async def test_duplicate_registration_is_rejected() -> None:
    @dispatcher.register(_echo)
    async def _first(_req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
        return _EchoResp(echoed="first")

    with pytest.raises(dispatcher.DuplicateCommand, match="拒绝"):

        @dispatcher.register(_echo)
        async def _second(_req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
            return _EchoResp(echoed="second")


async def test_reregistering_the_same_handler_is_idempotent() -> None:
    """模块重载会让同一函数再注册一次，这不该炸。"""

    async def _handle(_req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
        return _EchoResp(echoed="ok")

    dispatcher.register(_echo)(_handle)
    dispatcher.register(_echo)(_handle)
    assert await dispatcher.call(_echo, _EchoReq("x"), db=object()) == _EchoResp("ok")


async def test_wrong_request_type_is_rejected() -> None:
    @dispatcher.register(_echo)
    async def _handle(_req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
        return _EchoResp(echoed="ok")

    with pytest.raises(TypeError, match="请求类型"):
        await dispatcher.call(_echo, "not-a-request", db=object())


async def test_wrong_response_type_is_rejected() -> None:
    @dispatcher.register(_echo)
    async def _handle(_req: _EchoReq, *, db: AsyncSession) -> str:
        return "wrong"

    with pytest.raises(dispatcher.CommandResultMismatch, match="响应类型"):
        await dispatcher.call(_echo, _EchoReq("x"), db=object())


def test_reset_clears_registry() -> None:
    @dispatcher.register(_echo)
    async def _handle(_req: _EchoReq, *, db: AsyncSession) -> _EchoResp:
        return _EchoResp(echoed="ok")

    dispatcher.reset()
    assert not dispatcher.is_registered(_echo)
