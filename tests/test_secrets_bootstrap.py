"""M5 7.2.3 Infisical 启动拉取验收：开关语义、注入范围、失败降级/必失败。

用 httpx.MockTransport 模拟 universal-auth 登录 + secrets raw 接口，environ 传 dict，
不触网、不依赖自托管实例。
"""

from __future__ import annotations

import json
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


# ---- B6b：引导密钥走文件（零明文路径）----


def test_client_id_file_wins_over_env(tmp_path: Any) -> None:
    """文件优先于 env：登录用的是挂载文件里的凭据。"""
    cid_file = tmp_path / "cid"
    cid_file.write_text("file-cid\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/universal-auth/login"):
            seen["clientId"] = json.loads(request.content)["clientId"]
            return httpx.Response(200, json={"accessToken": "tok"})
        if request.url.path.endswith("/secrets/raw"):
            return httpx.Response(200, json={"secrets": []})
        return httpx.Response(404)

    env = dict(_BASE_ENV)
    env["LKM_INFISICAL_CLIENT_ID"] = "env-cid"  # 应被文件覆盖
    env["LKM_INFISICAL_CLIENT_ID_FILE"] = str(cid_file)

    assert bootstrap(env, client_factory=_factory(_handler)) == 0
    assert seen["clientId"] == "file-cid"


def test_missing_file_falls_back_to_env(tmp_path: Any) -> None:
    """配了路径但文件不在（未挂卷/本地开发）→ 回落 env，照常注入。"""
    env = dict(_BASE_ENV)
    env["LKM_INFISICAL_CLIENT_ID_FILE"] = str(tmp_path / "does-not-exist")

    assert bootstrap(env, client_factory=_factory(_ok_handler)) == 0
    assert env["LKM_JWT_SECRET"] == "real-jwt"


def test_empty_file_failopen_vs_fastfail(tmp_path: Any) -> None:
    """空文件是配置错：非必需降级、必需 fail-fast（都不触网）。"""
    empty = tmp_path / "empty"
    empty.write_text("   \n", encoding="utf-8")

    def _boom(_timeout: float) -> Any:
        raise AssertionError("凭据取自文件，配置错时不应建 client")

    env = dict(_BASE_ENV)
    env["LKM_INFISICAL_CLIENT_SECRET_FILE"] = str(empty)
    assert bootstrap(env, client_factory=_boom) == 0  # 非必需 → 降级

    required = dict(env, LKM_INFISICAL_REQUIRED="true")
    assert bootstrap(required, client_factory=_boom) == 1  # 必需 → fail-fast


def test_unreadable_path_fails_when_required(tmp_path: Any) -> None:
    """路径指向目录（读取必败）：按不可读处理，而不是静默回落 env。"""
    env = dict(
        _BASE_ENV,
        LKM_INFISICAL_REQUIRED="true",
        LKM_INFISICAL_CLIENT_SECRET_FILE=str(tmp_path),  # 目录
    )

    def _boom(_timeout: float) -> Any:
        raise AssertionError("配置错不该触网")

    assert bootstrap(env, client_factory=_boom) == 1
