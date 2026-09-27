"""T2：httpx 连接层重试 + 进程内熔断（蓝图 §5.4）的验收。

- CircuitBreaker 状态机：连续失败达阈值 → open（短路）；冷却后半开放行**一次**试探；试探失败
  重新 open、成功闭合。
- 接入 user_http：open 时**不再发出网络请求**（假 transport 计数证明短路）；5xx/网络错记失败，
  权威 404 **不**记失败；熔断 open 仍抛 ``UserHttpUnavailable``（fail-open/fail-closed 契约不变）。
- ``_build_client`` 启用连接层重试且不破坏 ``_client_factory`` 注入缝。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

import auth.user_http as user_http
from app.core.config import settings
from auth.circuit_breaker import CircuitBreaker, auth_http_breaker


class _Clock:
    """可拨动的单调时钟，隔离冷却计时。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _reset_breaker() -> AsyncIterator[None]:
    """每用例前后复位全局熔断器与注入缝，杜绝跨用例污染。"""
    auth_http_breaker.reset()
    user_http._client_factory = None  # type: ignore[attr-defined]
    yield
    auth_http_breaker.reset()
    user_http._client_factory = None  # type: ignore[attr-defined]


class TestCircuitBreakerStateMachine:
    def test_opens_after_consecutive_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 3)
        monkeypatch.setattr(settings, "auth_http_circuit_reset_s", 30.0)
        cb = CircuitBreaker(clock=_Clock())
        assert cb.allow() is True
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is True  # 未达阈值仍放行
        cb.record_failure()  # 第 3 次 → open
        assert cb.allow() is False  # 冷却期内短路

    def test_half_open_probe_then_reopen_or_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 1)
        monkeypatch.setattr(settings, "auth_http_circuit_reset_s", 30.0)
        clk = _Clock()
        cb = CircuitBreaker(clock=clk)
        assert cb.allow() is True
        cb.record_failure()  # open
        assert cb.allow() is False
        clk.now += 30.0
        assert cb.allow() is True  # 半开：放行一次试探
        assert cb.allow() is False  # 探针在途，其余继续短路
        cb.record_failure()  # 试探失败 → 重新 open 并重计冷却
        assert cb.allow() is False
        clk.now += 30.0
        assert cb.allow() is True
        cb.record_success()  # 试探成功 → 闭合复位
        assert cb.allow() is True

    def test_reset_clears_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 1)
        cb = CircuitBreaker(clock=_Clock())
        cb.record_failure()
        assert cb.allow() is False
        cb.reset()
        assert cb.allow() is True


def _enable_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_http_url", "http://auth-proc")
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret")
    monkeypatch.setattr(settings, "auth_http_circuit_reset_s", 300.0)


def _inject(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        user_http,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://auth-proc"),
    )


class TestUserHttpIntegration:
    async def test_open_short_circuits_without_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_seam(monkeypatch)
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 2)
        calls: list[httpx.Request] = []

        async def _boom(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectError("auth unreachable")

        _inject(monkeypatch, _boom)
        uid = uuid.uuid4()
        for _ in range(2):
            with pytest.raises(user_http.UserHttpUnavailable):
                await user_http.fetch_user_http_payload(uid)
        assert len(calls) == 2  # 两次都真发了请求
        # 已达阈值 → open：本次必须短路，不再新建 client / 发请求
        with pytest.raises(user_http.UserHttpUnavailable) as ei:
            await user_http.fetch_user_http_payload(uid)
        assert "circuit open" in str(ei.value)
        assert len(calls) == 2  # 计数不变 == 没发出网络请求

    async def test_404_is_authoritative_not_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """权威 404 不记失败：发 >阈值 次仍不熔断（否则「用户不存在」会把整条缝熔掉）。"""
        _enable_seam(monkeypatch)
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 2)
        calls: list[httpx.Request] = []

        async def _notfound(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(404, json={})

        _inject(monkeypatch, _notfound)
        for _ in range(5):
            assert await user_http.fetch_user_http_payload(uuid.uuid4()) == (None, None)
        assert len(calls) == 5  # 5 > 2 却从未短路 → 404 未计失败

    async def test_success_resets_failure_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_seam(monkeypatch)
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 3)
        mode: dict[str, int] = {"n": 0}

        async def _handler(request: httpx.Request) -> httpx.Response:
            mode["n"] += 1
            # 第 3 次 200（可达即算成功 → 计数清零）；4/5 再各失败一次也只累计到 2 < 3。
            # 若成功未复位，累计 4 次失败早该 open，下面的 allow() 就会是 False。
            return httpx.Response(500 if mode["n"] != 3 else 200, json={})

        _inject(monkeypatch, _handler)
        uid = uuid.uuid4()
        for _ in range(5):
            # 500 与 200-缺 data 都按 fail-open 契约抛 Unavailable，但只有 500/网络错记失败
            with pytest.raises(user_http.UserHttpUnavailable):
                await user_http.fetch_user_http_payload(uid)
        assert auth_http_breaker.allow() is True  # 计数已被中间那次成功复位

    async def test_circuit_open_keeps_fail_closed_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fail-closed 缝在熔断 open 时同样抛 Unavailable（调用方拒绝），绝非静默放行。"""
        _enable_seam(monkeypatch)
        monkeypatch.setattr(settings, "auth_http_circuit_failures", 1)

        async def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        _inject(monkeypatch, _boom)
        with pytest.raises(user_http.UserHttpUnavailable):
            await user_http.authorize_via_seam(
                user_id=uuid.uuid4(), expect_token_version=0, iat_ts=None
            )
        with pytest.raises(user_http.UserHttpUnavailable) as ei:
            await user_http.authorize_via_seam(
                user_id=uuid.uuid4(), expect_token_version=0, iat_ts=None
            )
        assert "circuit open" in str(ei.value)


class TestBuildClientRetries:
    async def test_connect_retries_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_retries", 4)
        monkeypatch.setattr(user_http, "_client_factory", None)
        client = user_http._build_client()
        try:
            transport = client._transport  # type: ignore[attr-defined]
            assert isinstance(transport, httpx.AsyncHTTPTransport)
            # httpcore 把 retries 存在 pool 上（白盒断言注入值确已生效）
            assert transport._pool._retries == 4  # type: ignore[attr-defined]
        finally:
            await client.aclose()

    async def test_injected_factory_still_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None))
        monkeypatch.setattr(user_http, "_client_factory", lambda: sentinel)
        assert user_http._build_client() is sentinel
        await sentinel.aclose()
