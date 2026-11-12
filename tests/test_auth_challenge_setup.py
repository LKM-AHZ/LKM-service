"""The challenge must hand off mandatory setup without issuing a session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from auth import challenge


async def test_setup_handoff_consumes_challenge_without_storing_temp(monkeypatch):
    consume = AsyncMock()
    monkeypatch.setattr(challenge, "_consume", consume)
    state = SimpleNamespace()
    payload = {
        "setup_required": True,
        "requires_2fa": True,
        "temp_token": "setup-only",
        "access_token": None,
    }
    result = await challenge._advance(None, state, factor="password", data=payload)
    assert result == {**payload, "status": "setup_required"}
    consume.assert_awaited_once_with(None, state, status="complete")
    assert not hasattr(state, "temp_token")
