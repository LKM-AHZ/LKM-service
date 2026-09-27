"""蓝图 §5.7：WS 心跳 pong 与重连 token 续期。

覆盖两层：
1. ``_should_keep_connection`` 纯判定：pong 保持、refresh 校验且**必须同一 user_id**、
   畸形消息忽略；
2. ``ws_events`` 主循环接线：pong 不关连接、refresh 换身份 → 用既有未授权策略关闭。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from app.ws import router as ws_router

_UID1 = uuid.UUID("00000000-0000-7000-8000-000000000001")
_UID2 = uuid.UUID("00000000-0000-7000-8000-000000000002")


@pytest.fixture
def fake_authorize(monkeypatch: pytest.MonkeyPatch) -> dict[str, uuid.UUID | None]:
    """把 ``_authorize`` 替成查表：token '-' 分隔后第一段是 user_id 或 'bad'。"""
    table: dict[str, uuid.UUID | None] = {}

    async def _fake(token: str) -> uuid.UUID | None:
        return table.get(token)

    monkeypatch.setattr(ws_router, "_authorize", _fake)
    return table


class TestControlMessageJudgement:
    async def test_pong_keeps_connection(self) -> None:
        assert await ws_router._should_keep_connection(
            json.dumps({"type": "pong"}), _UID1
        )

    async def test_malformed_text_is_ignored(self) -> None:
        for raw in ("not-json", "[]", json.dumps({"type": "unknown"})):
            assert await ws_router._should_keep_connection(raw, _UID1)

    async def test_refresh_same_user_keeps_connection(
        self, fake_authorize: dict[str, uuid.UUID | None]
    ) -> None:
        fake_authorize["new-token"] = _UID1
        raw = json.dumps({"type": "refresh", "token": "new-token"})
        assert await ws_router._should_keep_connection(raw, _UID1)

    async def test_refresh_other_user_is_rejected(
        self, fake_authorize: dict[str, uuid.UUID | None]
    ) -> None:
        """核心安全：不能借续期把连接换成另一个用户。"""
        fake_authorize["evil-token"] = _UID2
        raw = json.dumps({"type": "refresh", "token": "evil-token"})
        assert not await ws_router._should_keep_connection(raw, _UID1)

    async def test_refresh_invalid_token_is_rejected(
        self, fake_authorize: dict[str, uuid.UUID | None]
    ) -> None:
        raw = json.dumps({"type": "refresh", "token": "bad"})
        assert not await ws_router._should_keep_connection(raw, _UID1)

    async def test_refresh_missing_token_is_rejected(self) -> None:
        raw = json.dumps({"type": "refresh"})
        assert not await ws_router._should_keep_connection(raw, _UID1)


class _FakeWS:
    """按队列喂消息的假 WebSocket；队列耗尽即抛 WebSocketDisconnect 结束循环。"""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent: list[str] = []
        self.closed: int | None = None
        self.accepted = False
        self.query_params = {"token": "t", "channels": ""}

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect(1000)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


class _FakeManager:
    def __init__(self) -> None:
        self.unregistered = False

    async def register(self, *_a: Any, **_k: Any) -> None: ...

    async def ensure_subscription(self) -> None: ...

    async def unregister(self, *_a: Any, **_k: Any) -> None:
        self.unregistered = True


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> _FakeManager:
    m = _FakeManager()
    monkeypatch.setattr(ws_router, "manager", m)
    return m


async def test_loop_pong_does_not_close(
    fake_authorize: dict[str, uuid.UUID | None],
    fake_manager: _FakeManager,
) -> None:
    fake_authorize["t"] = _UID1
    ws = _FakeWS([json.dumps({"type": "pong"})])
    await ws_router.ws_events(ws)
    assert ws.closed is None  # pong 不关连接
    assert fake_manager.unregistered  # 正常收尾清理


async def test_loop_refresh_same_user_keeps_open(
    fake_authorize: dict[str, uuid.UUID | None],
    fake_manager: _FakeManager,
) -> None:
    fake_authorize["t"] = _UID1
    fake_authorize["new"] = _UID1
    ws = _FakeWS([json.dumps({"type": "refresh", "token": "new"})])
    await ws_router.ws_events(ws)
    assert ws.closed is None


async def test_loop_refresh_other_user_closes_unauthorized(
    fake_authorize: dict[str, uuid.UUID | None],
    fake_manager: _FakeManager,
) -> None:
    fake_authorize["t"] = _UID1
    fake_authorize["evil"] = _UID2
    ws = _FakeWS([json.dumps({"type": "refresh", "token": "evil"})])
    await ws_router.ws_events(ws)
    assert ws.closed == ws_router._UNAUTHORIZED_CLOSE
    assert fake_manager.unregistered
