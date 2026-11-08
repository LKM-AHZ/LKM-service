"""积分写入口端口（``core.ports.points``）的转发语义与 fail-fast。

端口的意义是「消费方不再 import points 实现」，所以这里守两件事：
1. 包装函数把**调用方的 session 原样**转给实现（同事务的前提）；
2. 未绑定时立刻 ``PortNotBound``，绝不静默丢账。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.ports import points as points_port
from core.ports.registry import PortNotBound


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def enqueue_event(
        self, db: Any, user_id: uuid.UUID, event: str, ref_id: str
    ) -> None:
        self.calls.append(("enqueue_event", db, user_id, event, ref_id))

    async def reward(
        self,
        db: Any,
        user_id: uuid.UUID,
        delta: int,
        reason: str,
        ref_type: str,
        ref_id: str,
        *,
        allow_negative: bool = False,
    ) -> Any:
        self.calls.append(
            ("reward", db, user_id, delta, reason, ref_type, ref_id, allow_negative)
        )
        return "entry"

    async def spend(
        self,
        db: Any,
        user_id: uuid.UUID,
        amount: int,
        reason: str,
        ref_type: str,
        ref_id: str,
    ) -> Any:
        self.calls.append(("spend", db, user_id, amount, reason, ref_type, ref_id))
        return "entry"


async def test_enqueue_event_forwards_the_caller_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _FakeWriter()
    monkeypatch.setattr(points_port, "get", lambda _name: writer)

    session = object()
    uid = uuid.uuid4()
    await points_port.enqueue_event(session, uid, "like", "item:1")

    assert writer.calls == [("enqueue_event", session, uid, "like", "item:1")]


async def test_reward_and_spend_forward_keyword_only_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _FakeWriter()
    monkeypatch.setattr(points_port, "get", lambda _name: writer)

    session = object()
    uid = uuid.uuid4()
    assert await points_port.reward(session, uid, 5, "post", "item", "1") == "entry"
    assert (
        await points_port.reward(
            session, uid, -5, "punish", "item", "2", allow_negative=True
        )
        == "entry"
    )
    assert await points_port.spend(session, uid, 3, "buy", "item", "3") == "entry"

    assert writer.calls[0][-1] is False
    assert writer.calls[1][-1] is True


async def test_unbound_port_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unbound(name: str) -> Any:
        raise PortNotBound(f"端口 {name!r} 未绑定")

    monkeypatch.setattr(points_port, "get", _unbound)

    with pytest.raises(PortNotBound):
        await points_port.enqueue_event(object(), uuid.uuid4(), "like", "item:1")
