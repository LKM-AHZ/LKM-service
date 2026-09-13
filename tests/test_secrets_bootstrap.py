"""M5 7.2.3 Infisical 启动拉取验收：开关语义、注入范围、失败降级/必失败。

用 httpx.MockTransport 模拟 universal-auth 登录 + secrets raw 接口，environ 传 dict，
不触网、不依赖自托管实例。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.secrets_bootstrap import bootstrap

_BASE_ENV: dict[str, str] = {
    "LKM_INFISICAL_ENABLED": "true",
    "LKM_INFISICAL_SITE_URL": "http://infisical.test",
    "LKM_INFISICAL_PROJECT_ID": "proj-1",
    "LKM_INFISICAL_ENVIRONMENT": "prod",
    "LKM_INFISICAL_SECRET_PATH": "/",
    "LKM_INFISICAL_CLIENT_ID": "cid",
    "LKM_INFISICAL_CLIENT_SECRET": "csecret",
    "LKM_INFISICAL_REQUIRED": "false",
}


def _factory(handler: Any) -> Any:
    def _make(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    return _make


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/universal-auth/login"):
        return httpx.Response(200, json={"accessToken": "tok"})
    if request.url.path.endswith("/secrets/raw"):
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "secrets": [
                    {"secretKey": "LKM_JWT_SECRET", "secretValue": "real-jwt"},
                    {"secretKey": "LKM_DB_PASSWORD", "secretValue": "real-db"},
                    {"secretKey": "SOME_OTHER", "secretValue": "ignored"},
                ]
            },
        )
    return httpx.Response(404)


def test_disabled_does_nothing() -> None:
    env = {"LKM_INFISICAL_ENABLED": "false", "LKM_JWT_SECRET": "keep"}

    def _boom(_timeout: float) -> Any:
        raise AssertionError("disabled 时不应建 client")

    assert bootstrap(env, client_factory=_boom) == 0
    assert env["LKM_JWT_SECRET"] == "keep"


def test_injects_lkm_secrets_and_keeps_existing() -> None:
    env = dict(_BASE_ENV)
    env["LKM_DB_PASSWORD"] = "explicit-wins"
    assert bootstrap(env, client_factory=_factory(_ok_handler)) == 0
    assert env["LKM_JWT_SECRET"] == "real-jwt"  # 缺失 → 注入
    assert env["LKM_DB_PASSWORD"] == "explicit-wins"  # 已存在 → 不覆盖
    assert "SOME_OTHER" not in env  # 非 LKM_ 前缀不入


def test_incomplete_config_fails_only_when_required() -> None:
    env = {"LKM_INFISICAL_ENABLED": "true"}  # 缺 site/project/凭据
    assert bootstrap(env, client_factory=_factory(_ok_handler)) == 0
    env["LKM_INFISICAL_REQUIRED"] = "true"
    assert bootstrap(env, client_factory=_factory(_ok_handler)) == 1


def test_fetch_failure_failopen_vs_fastfail() -> None:
    def _unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad creds"})

    factory = _factory(_unauthorized)
    env = dict(_BASE_ENV)
    assert bootstrap(env, client_factory=factory) == 0  # 非必需 → 降级
    assert "LKM_JWT_SECRET" not in env

    required = dict(_BASE_ENV)
    required["LKM_INFISICAL_REQUIRED"] = "true"
    assert bootstrap(required, client_factory=factory) == 1  # 必需 → fail-fast
