"""蓝图 §2 第 3 条：readiness 里软依赖「告知但不阻塞」。

硬约束：软依赖状态只体现为字段值，**绝不改变** 200/503 判定。默认单机配置（pg 检索 /
local 存储）下软探针必须短路为 disabled，不触网。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.modules.health.router as health_mod
from app.modules.health.router import DependencyStatus
from app.modules.health.router import router as health_router
from core.config import settings


@pytest.fixture
def probe_app() -> FastAPI:
    application = FastAPI()
    application.include_router(health_router)
    return application


@pytest.fixture
async def probe_client(probe_app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=probe_app), base_url="http://test"
    ) as c:
        yield c


def _stub_hard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db: str = "up",
    redis: str = "up",
    pulsar: str = "up",
    auth: str = "up",
) -> None:
    def _mk(status: str):
        async def _probe() -> DependencyStatus:
            return DependencyStatus(status=status, detail=f"stub-{status}")

        return _probe

    for name, status in (
        ("_probe_db", db),
        ("_probe_redis", redis),
        ("_probe_pulsar", pulsar),
        ("_probe_auth", auth),
        ("_probe_verify_key", "up"),
    ):
        monkeypatch.setattr(health_mod, name, _mk(status))


def _stub_soft(
    monkeypatch: pytest.MonkeyPatch, *, search: str = "disabled", storage: str = "disabled"
) -> None:
    def _mk(status: str):
        async def _probe() -> DependencyStatus:
            return DependencyStatus(status=status, detail=f"soft-{status}")

        return _probe

    monkeypatch.setattr(health_mod, "_probe_search", _mk(search))
    monkeypatch.setattr(health_mod, "_probe_storage", _mk(storage))


class TestReadinessSoftField:
    async def test_soft_present_and_hard_and_untouched(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_hard(monkeypatch)
        _stub_soft(monkeypatch)
        resp = await probe_client.get("/readiness")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert set(payload["soft"]) == {"search", "storage"}
        assert payload["soft"]["search"]["status"] == "disabled"

    async def test_soft_error_does_not_change_200(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """软依赖全 error，硬依赖全 up → 仍 200（软缺失可降级，不阻塞入流）。"""
        _stub_hard(monkeypatch)
        _stub_soft(monkeypatch, search="error", storage="error")
        resp = await probe_client.get("/readiness")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["soft"]["search"]["status"] == "error"

    async def test_soft_up_does_not_rescue_hard_failure(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """硬依赖 error 时即便软依赖全 up，也必须 503（软依赖无权提升就绪）。"""
        _stub_hard(monkeypatch, redis="error")
        _stub_soft(monkeypatch, search="up", storage="up")
        resp = await probe_client.get("/readiness")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"


class TestSoftProbes:
    async def test_search_disabled_on_default_pg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "search_engine", "pg")
        status = await health_mod._probe_search()
        assert status.status == "disabled"

    async def test_storage_disabled_on_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "storage_backend", "local")
        status = await health_mod._probe_storage()
        assert status.status == "disabled"

    async def test_search_up_and_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "search_engine", "opensearch")
        monkeypatch.setattr(settings, "search_opensearch_url", "http://os:9200")

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="{}")

        monkeypatch.setattr(
            health_mod,
            "_soft_probe_factory",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        assert (await health_mod._probe_search()).status == "up"

        def _boom(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(
            health_mod,
            "_soft_probe_factory",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(_boom)),
        )
        assert (await health_mod._probe_search()).status == "error"

    async def test_storage_reachable_on_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MinIO/S3 对匿名根 GET 常回 403/404——拿到响应即视为可达（up），不算故障。"""
        monkeypatch.setattr(settings, "storage_backend", "s3")
        monkeypatch.setattr(settings, "s3_endpoint_url", "http://minio:9000")

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        monkeypatch.setattr(
            health_mod,
            "_soft_probe_factory",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        assert (await health_mod._probe_storage()).status == "up"
