"""M6.2 探针分级验收：/liveness 零外部依赖、/readiness 复合且未就绪返 503、Pulsar 探活。

hermetic：单体探针经 monkeypatch 替换为固定回报（不真连 DB/Redis/AUTH）；Pulsar 探针走
httpx.MockTransport 注入的假 client（``pulsar_lag._probe_client_factory``），零真实网络。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.core.pulsar_lag as pulsar_lag
import app.modules.health.router as health_mod
from app.core.config import settings
from app.modules.health.router import DependencyStatus
from app.modules.health.router import router as health_router


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


def _stub_probes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db: str = "up",
    redis: str = "up",
    pulsar: str = "up",
    auth: str = "up",
) -> None:
    """把四项探子替换为固定回报（detail 带状态名便于断言）。"""

    def _mk(status: str):
        async def _probe() -> DependencyStatus:
            return DependencyStatus(status=status, detail=f"stub-{status}")

        return _probe

    monkeypatch.setattr(health_mod, "_probe_db", _mk(db))
    monkeypatch.setattr(health_mod, "_probe_redis", _mk(redis))
    monkeypatch.setattr(health_mod, "_probe_pulsar", _mk(pulsar))
    monkeypatch.setattr(health_mod, "_probe_auth", _mk(auth))


# ── liveness ───────────────────────────────────────────────────────────────


async def test_liveness_ok_without_external_deps(
    probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零外部依赖：四个探子全改为必炸，liveness 仍 200。"""

    async def _boom() -> DependencyStatus:
        raise AssertionError("liveness 不应触碰任何外部依赖")

    for name in ("_probe_db", "_probe_redis", "_probe_pulsar", "_probe_auth"):
        monkeypatch.setattr(health_mod, name, _boom)
    resp = await probe_client.get("/liveness")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "api"}


# ── readiness ──────────────────────────────────────────────────────────────


async def test_readiness_ok_when_all_deps_up(
    probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probes(monkeypatch)
    resp = await probe_client.get("/readiness")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert {k: payload[k]["status"] for k in ("db", "redis", "pulsar", "auth")} == {
        "db": "up",
        "redis": "up",
        "pulsar": "up",
        "auth": "up",
    }


@pytest.mark.parametrize("failing", ["db", "redis", "pulsar", "auth"])
async def test_readiness_503_when_any_hard_dep_errors(
    probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """任一硬依赖 error → 503（进程不重启由 /liveness 另行表达）。"""
    _stub_probes(monkeypatch, **{failing: "error"})
    resp = await probe_client.get("/readiness")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["status"] == "degraded"
    assert payload[failing]["status"] == "error"


@pytest.mark.parametrize("optional", ["pulsar", "auth"])
async def test_readiness_disabled_deps_do_not_block(
    probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, optional: str
) -> None:
    """未配置（disabled）的 Pulsar/AUTH 是部署取向而非故障，不降就绪。"""
    _stub_probes(monkeypatch, **{optional: "disabled"})
    resp = await probe_client.get("/readiness")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readiness_redis_disabled_is_degraded(
    probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 是硬依赖：未配置（disabled）亦不就绪——L2 缓存缺失时读一致性锚点不在。"""
    _stub_probes(monkeypatch, redis="disabled")
    resp = await probe_client.get("/readiness")
    assert resp.status_code == 503


async def test_readiness_db_disabled_is_degraded(
    probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probes(monkeypatch, db="disabled")
    assert (await probe_client.get("/readiness")).status_code == 503


async def test_probe_db_not_ready_before_schema_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 `_probe_db`：schema 未初始化时报 error——DB 可达但表未建 ≠ 就绪。

    启动不阻塞（lifespan 不再 await init_db）后这个窗口真实存在；只探 `SELECT 1` 会误报
    up，把流量放进一个查不了业务表的进程。故不应触达引擎，直接判未就绪。
    """
    monkeypatch.setattr(health_mod, "is_db_initialized", lambda: False)

    def _boom() -> object:
        raise AssertionError("schema 未就绪时不应触达引擎")

    monkeypatch.setattr(health_mod, "get_async_engine", _boom)
    status = await health_mod._probe_db()
    assert status.status == "error"
    assert status.detail == "schema not initialized"


# ── Pulsar 探活（pulsar_lag.probe_health）───────────────────────────────────


def _fake_probe_client(status_code: int, text: str) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=text)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def pulsar_env(monkeypatch: pytest.MonkeyPatch):
    """启用消息总线 + Admin REST 基址，并复位探针缓存（模块级全局，逐测隔离）。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://pulsar:6650")
    monkeypatch.setattr(settings, "pulsar_admin_url", "http://pulsar:8080")
    monkeypatch.setattr(pulsar_lag, "_probe_cache", None)
    yield
    pulsar_lag._probe_cache = None


async def test_pulsar_probe_disabled_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pulsar_url", "")
    monkeypatch.setattr(settings, "pulsar_admin_url", "")
    assert await pulsar_lag.probe_health() == ("disabled", "pulsar 未配置")


async def test_pulsar_probe_up_on_ok(
    pulsar_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pulsar_lag, "_probe_client_factory", _fake_probe_client(200, "ok")
    )
    assert await pulsar_lag.probe_health() == ("up", None)


async def test_pulsar_probe_error_on_non_ok_body(
    pulsar_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pulsar_lag, "_probe_client_factory", _fake_probe_client(200, "not-serving")
    )
    status, detail = await pulsar_lag.probe_health()
    assert status == "error"
    assert "not-serving" in (detail or "")


async def test_pulsar_probe_error_on_http_failure(
    pulsar_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pulsar_lag, "_probe_client_factory", _fake_probe_client(503, "")
    )
    status, detail = await pulsar_lag.probe_health()
    assert status == "error"
    assert "503" in (detail or "")


async def test_pulsar_probe_error_when_unreachable(
    pulsar_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不可达 → 转 error 返回，不向调用方抛（就绪探针不因底层抖动 500）。"""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        pulsar_lag,
        "_probe_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    status, detail = await pulsar_lag.probe_health()
    assert status == "error"
    assert "不可达" in (detail or "")


async def test_pulsar_probe_caches_only_success(
    pulsar_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功结果短缓存（少打 Admin REST）；失败不缓存（恢复立即可见）。"""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(
        pulsar_lag,
        "_probe_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (await pulsar_lag.probe_health())[0] == "up"
    assert (await pulsar_lag.probe_health())[0] == "up"
    assert calls == 1  # 第二次命中缓存

    monkeypatch.setattr(
        pulsar_lag, "_probe_client_factory", _fake_probe_client(500, "")
    )
    assert (await pulsar_lag.probe_health())[0] == "up"  # 仍在缓存窗口内
    pulsar_lag._probe_cache = None
    assert (await pulsar_lag.probe_health())[0] == "error"
    assert (await pulsar_lag.probe_health())[0] == "error"  # 失败不复用缓存，真重探
    assert pulsar_lag._probe_cache is None
