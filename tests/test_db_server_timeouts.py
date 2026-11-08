"""服务端超时的接线测试：settings → 建池参数 → asyncpg connect_args。

这类「参数逐层透传」最容易在重构里静默丢失（丢了下发，行为退回无超时，但没有任何
报错）。这里不连库，只断言透传链路。
"""

from __future__ import annotations

import pytest

from core import config as config_mod
from core.db import session as session_mod
from core.db.session import create_realm_async_engine


@pytest.fixture
def captured_engine_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """把 create_async_engine 换成记录器，避免真的建引擎/连库。"""
    captured: dict[str, object] = {}

    def fake_create(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session_mod, "create_async_engine", fake_create)
    return captured


def test_connect_args_carry_server_side_timeouts(
    captured_engine_kwargs: dict[str, object],
) -> None:
    create_realm_async_engine(
        "postgresql+asyncpg://u:p@localhost:5432/db",
        statement_timeout_ms=1500,
        idle_tx_timeout_ms=2500,
    )
    connect_args = captured_engine_kwargs["connect_args"]
    assert isinstance(connect_args, dict)
    assert connect_args["server_settings"] == {
        "statement_timeout": "1500",
        "idle_in_transaction_session_timeout": "2500",
    }


def test_zero_disables_server_side_timeouts(
    captured_engine_kwargs: dict[str, object],
) -> None:
    create_realm_async_engine(
        "postgresql+asyncpg://u:p@localhost:5432/db",
        statement_timeout_ms=0,
        idle_tx_timeout_ms=0,
    )
    assert captured_engine_kwargs["connect_args"] == {}


def test_web_engine_wires_settings_into_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    # `_async_engine` 用 monkeypatch 置空，测试结束自动恢复，避免把假引擎留给后续用例。
    monkeypatch.setattr(session_mod, "_async_engine", None)
    monkeypatch.setattr(session_mod, "create_realm_async_engine", fake_engine)

    assert session_mod.get_async_engine() is not None
    settings = config_mod.settings
    assert captured["statement_timeout_ms"] == settings.db_statement_timeout_ms
    assert captured["idle_tx_timeout_ms"] == settings.db_idle_tx_timeout_ms
