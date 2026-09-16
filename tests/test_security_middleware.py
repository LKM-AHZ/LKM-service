"""M6.1 公网安全面验收：安全响应头 / CORS 白名单 / HSTS 门控 / TrustedHost 白名单。

hermetic：自建最小 FastAPI 应用就地装配 ``install_security_middleware``，不触碰真实
DB/Redis/总线；``settings`` 逐测 monkeypatch 后复原，不污染其它用例。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import middleware
from app.core.config import settings
from app.core.middleware import _HSTS_VALUE, install_security_middleware

_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}
_PERMISSIONS_POLICY = "geolocation=(), microphone=(), camera=()"


@pytest.fixture
def sec_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把安全面配置复位为 dev 缺省（白名单空 → allowed_hosts='*'，CORS 取本地兜底）。"""
    for key, value in (
        ("env", "dev"),
        ("allowed_hosts", ""),
        ("cors_origins", ""),
    ):
        monkeypatch.setattr(settings, key, value)


def _make_app() -> FastAPI:
    application = FastAPI()

    @application.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    install_security_middleware(application)
    return application


@asynccontextmanager
async def _client(
    app: FastAPI, base_url: str = "http://test"
) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as c:
        yield c


# ── 安全响应头 ──────────────────────────────────────────────────────────────


async def test_security_headers_on_normal_response(sec_env) -> None:
    app = _make_app()
    async with _client(app) as c:
        resp = await c.get("/ping")
    assert resp.status_code == 200
    for name, value in _SECURITY_HEADERS.items():
        assert resp.headers[name] == value
    assert resp.headers["permissions-policy"] == _PERMISSIONS_POLICY
    # dev 非生产 → 绝不下发 HSTS（纯 HTTP 环境会锁死域名）
    assert "strict-transport-security" not in resp.headers


async def test_hsts_enabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "allowed_hosts", "lkm-ahz.ltd")
    monkeypatch.setattr(settings, "cors_origins", "https://lkm-ahz.ltd")
    app = _make_app()
    async with _client(app, base_url="http://lkm-ahz.ltd") as c:
        resp = await c.get("/ping")
    assert resp.status_code == 200
    assert resp.headers["strict-transport-security"] == _HSTS_VALUE


async def test_security_headers_applied_on_rejected_host(sec_env, monkeypatch) -> None:
    """安全头在最外层：TrustedHost 的 400 拒答也带安全头。"""
    monkeypatch.setattr(settings, "allowed_hosts", "lkm-ahz.ltd")
    app = _make_app()
    async with _client(app, base_url="http://evil.example") as c:
        resp = await c.get("/ping")
    assert resp.status_code == 400
    for name, value in _SECURITY_HEADERS.items():
        assert resp.headers[name] == value


# ── TrustedHost ────────────────────────────────────────────────────────────


async def test_trusted_host_allows_listed_and_rejects_others(
    sec_env, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "allowed_hosts", "lkm-ahz.ltd,backend,127.0.0.1")
    app = _make_app()
    for host, expected in (
        ("http://lkm-ahz.ltd", 200),
        # 端口须被剥离后再比对（容器 healthcheck 直连 127.0.0.1:8000）
        ("http://127.0.0.1:8000", 200),
        ("http://evil.example", 400),
    ):
        async with _client(app, base_url=host) as c:
            assert (await c.get("/ping")).status_code == expected


async def test_allowed_hosts_empty_means_wildcard(sec_env) -> None:
    assert settings.allowed_hosts_list == ["*"]
    app = _make_app()
    async with _client(app, base_url="http://anything.example") as c:
        assert (await c.get("/ping")).status_code == 200


# ── CORS 白名单 ────────────────────────────────────────────────────────────


async def test_cors_allows_whitelisted_origin_with_credentials(
    sec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cors_origins", "https://lkm-ahz.ltd")
    app = _make_app()
    async with _client(app) as c:
        resp = await c.get("/ping", headers={"Origin": "https://lkm-ahz.ltd"})
    assert resp.headers["access-control-allow-origin"] == "https://lkm-ahz.ltd"
    assert resp.headers["access-control-allow-credentials"] == "true"
    assert "X-Request-ID" in resp.headers["access-control-expose-headers"]


async def test_cors_rejects_unknown_origin(
    sec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cors_origins", "https://lkm-ahz.ltd")
    app = _make_app()
    async with _client(app) as c:
        resp = await c.get("/ping", headers={"Origin": "https://evil.example"})
    # 响应本体仍返回（CORS 由浏览器侧拦截），但不回任何放行头
    assert "access-control-allow-origin" not in resp.headers


async def test_cors_preflight_rejected_for_unknown_origin(
    sec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cors_origins", "https://lkm-ahz.ltd")
    app = _make_app()
    async with _client(app) as c:
        resp = await c.options(
            "/ping",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


async def test_cors_preflight_allowed_for_whitelisted_origin(
    sec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cors_origins", "https://lkm-ahz.ltd")
    app = _make_app()
    async with _client(app) as c:
        resp = await c.options(
            "/ping",
            headers={
                "Origin": "https://lkm-ahz.ltd",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://lkm-ahz.ltd"


async def test_cors_wildcard_disables_credentials(
    sec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """禁「* + 凭证」并存：命中通配即自动关闭 allow_credentials。"""
    monkeypatch.setattr(settings, "cors_origins", "*")
    app = _make_app()
    async with _client(app) as c:
        resp = await c.get("/ping", headers={"Origin": "https://anywhere.example"})
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in resp.headers


async def test_cors_origins_dev_fallback(sec_env) -> None:
    assert "http://localhost:4321" in settings.cors_origins_list


# ── 生产必填门控 ───────────────────────────────────────────────────────────


def test_production_requires_web_security_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "allowed_hosts", "")
    monkeypatch.setattr(settings, "cors_origins", "")
    with pytest.raises(ValueError, match="LKM_ALLOWED_HOSTS"):
        install_security_middleware(FastAPI())


def test_production_passes_with_both_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "allowed_hosts", "lkm-ahz.ltd")
    monkeypatch.setattr(settings, "cors_origins", "https://lkm-ahz.ltd")
    install_security_middleware(FastAPI())  # 不抛即通过


def test_dev_passes_without_web_security_config(sec_env) -> None:
    install_security_middleware(FastAPI())  # dev 零配置可跑


# ── 装配形态 ───────────────────────────────────────────────────────────────


def test_monolith_and_auth_apps_install_security_middleware() -> None:
    """单体与 auth 进程都已装上三件中间件（防只在一边装配导致语义漂移）。"""
    import app.main
    import app.main_auth

    expected = {
        middleware.TrustedHostMiddleware,
        middleware.CORSMiddleware,
        middleware.SecurityHeadersMiddleware,
    }
    for application in (app.main.app, app.main_auth.app):
        installed = {cls for cls, _args, _kw in application.user_middleware}
        assert expected <= installed


async def test_monolith_exposes_probe_endpoints(sec_env) -> None:
    """单体真实应用上 /api/v1/liveness 可达（零外部依赖，无需 lifespan/DB）。"""
    import app.main

    async with _client(app.main.app) as c:
        resp = await c.get("/api/v1/liveness")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "api"}


async def test_monolith_readiness_route_wired(
    sec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """readiness 已聚合进单体（探子替换为固定回报，验证路由可达 + 503 语义）。"""
    import app.main
    import app.modules.health.router as health_mod
    from app.modules.health.router import DependencyStatus

    async def _up() -> DependencyStatus:
        return DependencyStatus(status="up")

    async def _err() -> DependencyStatus:
        return DependencyStatus(status="error", detail="stub")

    monkeypatch.setattr(health_mod, "_probe_db", _err)
    monkeypatch.setattr(health_mod, "_probe_redis", _up)
    monkeypatch.setattr(health_mod, "_probe_pulsar", _up)
    monkeypatch.setattr(health_mod, "_probe_auth", _up)

    async with _client(app.main.app) as c:
        resp = await c.get("/api/v1/readiness")
    assert resp.status_code == 503
    assert resp.json()["db"]["status"] == "error"
