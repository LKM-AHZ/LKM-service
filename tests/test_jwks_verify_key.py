"""验签公钥的运行期获取与就绪上报（蓝图 §2 第 2 条）。

蓝图要求：验签公钥**优先从本地注入的密钥加载**，或**从 AUTH ``/jwks`` 拉取并缓存**；公钥未就绪
时主服务进入「验证暂不可用」状态——**不影响进程存活**，但 readiness 要**如实上报**。

覆盖：
- JWK → PEM 转换（n/e → SubjectPublicKeyInfo）；
- ``verification_status`` 两档（本地有钥可用 / 拿不到公钥即不可用）；
- ``refresh_public_key_from_jwks`` 的本地优先、未配 URL、非 200、无 RSA 成员、成功拉取五种路径；
- 拉到的公钥**真的能验签**（签一枚 RS256 token，用拉来的公钥 decode 成功）；
- readiness：公钥不可用时 503、可用时 200，且 ``verify_key`` 如实出现在响应里。

全程离线：出站走 ``jwt_keys._jwks_client_factory`` 注入的 ``httpx.MockTransport``。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

import auth.jwt_keys as jk
from app.modules.health import router as health_mod
from core.config import settings

_AUD = "lkm:web"


@pytest.fixture(autouse=True)
def _clean_key_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐测复位密钥相关配置与运行期缓存，避免相互污染。"""
    monkeypatch.setattr(settings, "jwt_private_key", None)
    monkeypatch.setattr(settings, "jwt_public_key", None)
    monkeypatch.setattr(settings, "jwt_private_key_file", "")
    monkeypatch.setattr(settings, "jwt_public_key_file", "")
    monkeypatch.setattr(settings, "auth_http_url", "")
    monkeypatch.setattr(jk, "_fetched_public_pem", None)
    monkeypatch.setattr(jk, "_jwks_client_factory", None)


def _keypair() -> tuple[rsa.RSAPrivateKey, str]:
    """生成一对 RSA 密钥，返回 ``(私钥对象, 公钥 PEM)``。"""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, pem.decode("ascii")


def _jwks_for(private: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"use": "sig", "alg": "RS256", "kid": "test"})
    return {"keys": [jwk]}


def _install_client(monkeypatch: pytest.MonkeyPatch, body: Any, status: int = 200) -> None:
    """把 JWKS 出站换成 MockTransport（离线、无网络）。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        if not isinstance(body, str):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    monkeypatch.setattr(
        jk,
        "_jwks_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ── JWK → PEM ─────────────────────────────────────────────────────────────


class TestJwkToPem:
    def should_roundtrip_public_numbers(self) -> None:
        private, _ = _keypair()
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        pem = jk._jwk_to_pem(jwk)
        loaded = serialization.load_pem_public_key(pem.encode())
        assert isinstance(loaded, rsa.RSAPublicKey)
        assert loaded.public_numbers() == private.public_key().public_numbers()

    def should_handle_unpadded_base64url(self) -> None:
        """JWK 的 n/e 无 base64 填充：长度不是 4 的倍数时也必须解得出。"""
        private, _ = _keypair()
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        assert "=" not in jwk["n"]
        assert jk._jwk_to_pem(jwk)


# ── verification_status ───────────────────────────────────────────────────


class TestVerificationStatus:
    def should_be_unavailable_without_any_key(self) -> None:
        """RS256-only：无任何 RSA 密钥即拿不到公钥，验签不可用。"""
        assert jk.verification_status() == "unavailable"

    def should_be_ok_with_local_public_key(self, monkeypatch) -> None:
        _, pem = _keypair()
        monkeypatch.setattr(settings, "jwt_public_key", SecretStr(pem))
        assert jk.verification_status() == "ok"

    def should_be_ok_with_private_key_only(self, monkeypatch) -> None:
        """只持私钥的签发侧：公钥可推导，验签天然可用。"""
        private, _ = _keypair()
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        monkeypatch.setattr(settings, "jwt_private_key", SecretStr(pem))
        assert jk.verification_status() == "ok"

    def should_not_raise_on_broken_config(self, monkeypatch) -> None:
        """配置成「有钥但内容为空」时 ``_pem`` 会抛——状态判定必须吞掉，探针不能 500。"""
        monkeypatch.setattr(settings, "jwt_public_key", SecretStr(""))
        monkeypatch.setattr(settings, "jwt_public_key_file", "/nonexistent/key.pem")
        assert jk.verification_status() == "unavailable"


# ── refresh_public_key_from_jwks ──────────────────────────────────────────


class TestRefreshFromJwks:
    async def should_skip_when_local_key_exists(self, monkeypatch) -> None:
        """本地优先：有本地公钥时**不发请求**（把出站换成会失败的 transport 来证明）。"""
        _, pem = _keypair()

        def _boom(*_a: Any, **_k: Any) -> httpx.AsyncClient:
            raise AssertionError("本地有公钥时不应发起 JWKS 请求")

        monkeypatch.setattr(settings, "jwt_public_key", SecretStr(pem))
        monkeypatch.setattr(jk, "_jwks_client_factory", _boom)
        assert await jk.refresh_public_key_from_jwks() is True

    async def should_return_false_without_auth_url(self) -> None:
        assert await jk.refresh_public_key_from_jwks() is False

    async def should_fetch_and_verify_a_token(self, monkeypatch) -> None:
        """端到端：JWKS → 缓存 → ``public_key()`` → 真的能验一枚 RS256 token。"""
        private, _ = _keypair()
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")
        _install_client(monkeypatch, _jwks_for(private))

        assert jk.verification_status() == "unavailable"  # 拉之前：不可用
        assert await jk.refresh_public_key_from_jwks() is True
        assert jk.verification_status() == "ok"

        token = jwt.encode({"sub": "u1", "aud": _AUD}, private, algorithm="RS256")
        assert jk.decode(token, audience=_AUD)["sub"] == "u1"

    async def should_return_false_on_non_200(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")
        _install_client(monkeypatch, "", status=503)
        assert await jk.refresh_public_key_from_jwks() is False

    async def should_return_false_without_rsa_member(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")
        _install_client(monkeypatch, {"keys": [{"kty": "EC", "crv": "P-256"}]})
        assert await jk.refresh_public_key_from_jwks() is False

    async def should_return_false_on_empty_keys(self, monkeypatch) -> None:
        """AUTH 未配公钥时 JWKS 返回空 keys（端点仍 200）——不算拉到。"""
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")
        _install_client(monkeypatch, {"keys": []})
        assert await jk.refresh_public_key_from_jwks() is False

    async def should_fail_open_on_transport_error(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("auth unreachable")

        monkeypatch.setattr(
            jk,
            "_jwks_client_factory",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert await jk.refresh_public_key_from_jwks() is False  # 不抛

    async def should_fail_open_on_malformed_body(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")
        _install_client(monkeypatch, json.dumps({"keys": "not-a-list"}))
        assert await jk.refresh_public_key_from_jwks() is False


# ── readiness 如实上报 ─────────────────────────────────────────────────────


@pytest.fixture
def probe_app() -> FastAPI:
    application = FastAPI()
    application.include_router(health_mod.router)
    return application


async def _readiness(app: FastAPI) -> httpx.Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/readiness")


@pytest.fixture
def _stub_other_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """其余硬依赖一律置 up：本文件只关心 verify_key 这一档。"""

    def _mk(status: str):
        async def _probe() -> health_mod.DependencyStatus:
            return health_mod.DependencyStatus(status=status)

        return _probe

    for name in ("_probe_db", "_probe_redis", "_probe_pulsar", "_probe_auth"):
        monkeypatch.setattr(health_mod, name, _mk("up"))


class TestReadinessReportsVerifyKey:
    async def should_be_ready_when_key_available(
        self, probe_app, _stub_other_probes, monkeypatch
    ) -> None:
        _, pem = _keypair()
        monkeypatch.setattr(settings, "jwt_public_key", SecretStr(pem))
        resp = await _readiness(probe_app)
        assert resp.status_code == 200
        assert resp.json()["verify_key"]["status"] == "up"

    async def should_be_503_when_rs256_key_missing(
        self, probe_app, _stub_other_probes, monkeypatch
    ) -> None:
        """拿不到公钥：验签为「暂不可用」→ 不入流（503），但进程仍然活着。"""
        resp = await _readiness(probe_app)
        assert resp.status_code == 503
        body = resp.json()
        assert body["verify_key"]["status"] == "error"
        # 匿名可读端点不回显内网信息
        assert "auth" not in (body["verify_key"]["detail"] or "")

    async def should_self_heal_when_jwks_becomes_available(
        self, probe_app, _stub_other_probes, monkeypatch
    ) -> None:
        """就绪探测会**就地拉一次** JWKS：AUTH 晚起也能在不重启的情况下被接上。"""
        private, _ = _keypair()
        monkeypatch.setattr(settings, "auth_http_url", "http://auth:8001")
        _install_client(monkeypatch, _jwks_for(private))

        resp = await _readiness(probe_app)
        assert resp.status_code == 200
        assert resp.json()["verify_key"]["status"] == "up"
