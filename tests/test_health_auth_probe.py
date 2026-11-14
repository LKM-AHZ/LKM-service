"""就绪 AUTH 依赖探针（双形态退役后恒启用）。

范围：``app/modules/health/router`` 的 ``_probe_auth`` 及其对 ``/health`` overall 的聚合。

- 独立 AUTH 进程是唯一认证真值：``auth_http_url`` 未配置即 error（不再有 disabled 形态）。
- 就绪反映 AUTH /liveness 活性；不可达/超时/非 ok → error 降级，探针自身**绝不抛出**，
  也不让一个不可达 AUTH 挂死整体就绪。

HTTP 全部经 httpx.MockTransport 注入出站，hermetic 零真实网络。fixture/复位范式沿用仓库。
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

import app.modules.health.router as health_mod
from app.modules.health.router import DependencyStatus
from core.config import settings


@pytest.fixture(autouse=True)
async def _reset_factory() -> AsyncIterator[None]:
    """每用例前后复位注入式出站 client 工厂与 config，杜绝跨用例残留。"""
    health_mod._auth_liveness_factory = None  # type: ignore[attr-defined]
    yield
    health_mod._auth_liveness_factory = None  # type: ignore[attr-defined]


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    """flag ON：配置 auth_http_url（独立 AUTH 进程接出）。"""
    monkeypatch.setattr(settings, "auth_http_url", "http://auth-svc:8001")
    monkeypatch.setattr(settings, "auth_http_timeout_s", 1.0)


def _off(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟未配置 auth 服务地址。"""
    monkeypatch.setattr(settings, "auth_http_url", "")


def _inject_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """用 httpx.MockTransport 替换 _probe_auth 出站，离线端到端驱动（真 httpx 解析）。"""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        health_mod,
        "_auth_liveness_factory",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://auth-svc:8001"),
    )


# ---- 探子级：_probe_auth 语义 ----
class TestProbeAuth:
    async def test_error_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auth_http_url 空 → error（auth 是必需依赖，不再有 disabled 形态）。"""
        _off(monkeypatch)
        deps = await health_mod._probe_auth()
        assert deps.status == "error"
        assert deps.detail

    async def test_up_when_liveness_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """flag ON + AUTH /liveness 200 ok → up。请求打到 <url>/liveness。"""
        _on(monkeypatch)
        captured: list[httpx.Request] = []

        async def _ok(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"status": "ok"})

        _inject_transport(monkeypatch, _ok)
        deps = await health_mod._probe_auth()
        assert deps.status == "up"
        assert captured and captured[0].url.path == "/liveness"

    async def test_error_on_connect_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auth 不可达(网络错) → error 降级，不抛（绝不因 AUTH 抖动炸就绪面）。"""
        _on(monkeypatch)

        async def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("auth unreachable")

        _inject_transport(monkeypatch, _boom)
        deps = await health_mod._probe_auth()
        assert deps.status == "error"
        assert deps.detail

    async def test_error_on_non_ok_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AUTH /liveness 非 200(如 503) → error。"""
        _on(monkeypatch)

        async def _non_ok(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        _inject_transport(monkeypatch, _non_ok)
        deps = await health_mod._probe_auth()
        assert deps.status == "error"

    async def test_error_on_liveness_not_ok_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AUTH /liveness 200 但 body 非 {status:ok} → error(不把 200 一律当 up)。"""
        _on(monkeypatch)

        async def _bad_body(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "degraded"})

        _inject_transport(monkeypatch, _bad_body)
        deps = await health_mod._probe_auth()
        assert deps.status == "error"


# ---- 端点级聚合：/health overall 反映 auth（仅 flag ON 参与）----
class TestHealthAggregation:
    async def test_unconfigured_auth_degrades(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未配置 auth 服务：db/redis up 也判 degraded。"""
        _off(monkeypatch)

        async def _up() -> DependencyStatus:
            return DependencyStatus(status="up")

        monkeypatch.setattr(health_mod, "_probe_db", _up)
        monkeypatch.setattr(health_mod, "_probe_redis", _up)
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        p: dict = resp.json()
        assert p["data"]["auth"]["status"] == "error"
        assert p["data"]["status"] == "degraded"

    async def test_flag_on_auth_error_degrades(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag ON + auth 不可达 → /health degraded(即便 db/redis up)，就绪反映 auth。"""
        _on(monkeypatch)

        async def _up() -> DependencyStatus:
            return DependencyStatus(status="up")

        async def _auth_err() -> DependencyStatus:
            return DependencyStatus(status="error", detail="auth 不可达")

        monkeypatch.setattr(health_mod, "_probe_db", _up)
        monkeypatch.setattr(health_mod, "_probe_redis", _up)
        monkeypatch.setattr(health_mod, "_probe_auth", _auth_err)
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        p: dict = resp.json()
        assert p["data"]["auth"]["status"] == "error"
        assert p["data"]["status"] == "degraded"

    async def test_flag_on_auth_up_overall_ok(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag ON + auth /liveness ok → /health ok：就绪反映 auth，一并保持其余 up。"""
        _on(monkeypatch)

        async def _up() -> DependencyStatus:
            return DependencyStatus(status="up")

        async def _liveness_ok(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        monkeypatch.setattr(health_mod, "_probe_db", _up)
        monkeypatch.setattr(health_mod, "_probe_redis", _up)
        _inject_transport(monkeypatch, _liveness_ok)  # probe_auth 走真实传输层
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        p: dict = resp.json()
        assert p["data"]["auth"]["status"] == "up"
        assert p["data"]["db"]["status"] == "up"
        assert p["data"]["status"] == "ok"
