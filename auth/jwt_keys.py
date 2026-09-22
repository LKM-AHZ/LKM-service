"""JWT 密钥与签发/验签原语（批 5：HS256 → RS256 + JWKS）。

蓝图 §4.2 定案「演进目标 RS256 非对称」：AUTH 用私钥签发，主服务与网关用公钥验签，
使验签方不再持有可签发的密钥。

**密钥来源**：``LKM_JWT_PRIVATE_KEY`` / ``LKM_JWT_PUBLIC_KEY``（PEM，经 Infisical/Secret
注入）。两者都留空则维持既有 HS256 行为（本地开发与测试的默认路径）；只给公钥可做
「只验不签」的验签方部署。

**双验签灰度**（蓝图给的时序：HS+RS 并存 → 灰度 ≥7 天 → 关 HS）：``LKM_JWT_HS_FALLBACK``
默认 true，RS256 生效后仍接受 HS256 旧 token；存量 token 清空后置 false 即关闭。
本仓因批 1 已重建库、token 全失效，**实际无存量需要灰度**，可直接置 false（登记 §8）。

**算法绑定**：验签按 token 头部的 ``alg`` 分支选密钥，且每支只放行单一算法——不做
「同一把密钥按算法列表逐个试」，避免 alg confusion（用公钥当 HMAC 密钥的经典混淆）。
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.core.secrets import reveal

RS256 = "RS256"
HS256 = "HS256"

#: APISIX 网关消费者的 ``key`` 必须与 token 里该 claim 相同（jwt-auth 靠它查消费者）。
#: 「网关可验签但不可签发」的取舍与 schema 细节见路线图 §8 #43。
GATEWAY_KEY = "lkm"


def _pem(value: Any, path: str = "") -> str | None:
    """取 PEM 文本：内联（SecretStr）优先，其次读 ``path`` 指向的文件；都没有则 ``None``。

    文件形态是为 k8s Secret 卷挂载与 compose 只读挂载准备的——多行 PEM 塞进 env 变量
    在 env_file / Secret 两侧都容易出转义问题。读不到文件直接抛（属部署配置错误，
    静默降级成「无密钥」会让 RS256 悄悄退回 HS256，比启动失败更危险）。
    """
    if value is not None:
        text = reveal(value).strip()
        if text:
            return text
        if not path:
            # 显式配了却解析为空（Secret/env 注入成空串——常见于密钥卷没挂上）：
            # 静默返回 None 会让 RS256 悄悄退回 HS256 签名，而只验签的网关/backend
            # 拿不到私钥推导的公钥，全线 401；比启动期直接失败危险得多。
            raise RuntimeError(
                "JWT key is configured but empty (blank PEM value and no *_file path)"
            )
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"无法读取 JWT 密钥文件 {path}: {exc}") from exc
        return text or None
    return None


@functools.lru_cache(maxsize=4)
def _load_private(pem: str) -> rsa.RSAPrivateKey:
    """按 PEM 内容缓存解析结果——以文本为键，设置变更即自然失效（不跨测试残留）。"""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("LKM_JWT_PRIVATE_KEY must be an RSA private key (PEM)")
    return key


@functools.lru_cache(maxsize=4)
def _load_public(pem: str) -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("LKM_JWT_PUBLIC_KEY must be an RSA public key (PEM)")
    return key


def signing_algorithm() -> str:
    """当前签发算法：配了私钥即 RS256，否则沿用 ``jwt_algorithm``（默认 HS256）。"""
    if _pem(settings.jwt_private_key, settings.jwt_private_key_file) is not None:
        return RS256
    return settings.jwt_algorithm


def public_key() -> rsa.RSAPublicKey | None:
    """验签公钥：优先用显式配置的 ``jwt_public_key``，否则由私钥推导；都没有则 ``None``。"""
    pem = _pem(settings.jwt_public_key, settings.jwt_public_key_file)
    if pem is not None:
        return _load_public(pem)
    private_pem = _pem(settings.jwt_private_key, settings.jwt_private_key_file)
    if private_pem is not None:
        return _load_private(private_pem).public_key()
    return None


# ─────────────────────── 签发 / 验签 ───────────────────────


def encode(payload: dict[str, Any]) -> str:
    """按 :func:`signing_algorithm` 签发。"""
    if signing_algorithm() == RS256:
        private_pem = _pem(settings.jwt_private_key, settings.jwt_private_key_file)
        # 不能用 assert：signing_algorithm() 只保证「有私钥 或 jwt_algorithm==RS256」，
        # 显式配 LKM_JWT_ALGORITHM=RS256 而未配私钥时这里就是 None；断言还会在
        # python -O 下被剥掉，退化成 _load_private(None) 的 AttributeError。
        if private_pem is None:
            raise RuntimeError(
                "JWT 签发算法为 RS256 但未配置 RSA 私钥（LKM_JWT_PRIVATE_KEY/_FILE）"
            )
        return jwt.encode(payload, _load_private(private_pem), algorithm=RS256)
    return jwt.encode(
        payload, reveal(settings.jwt_secret), algorithm=settings.jwt_algorithm
    )


def decode(token: str, *, audience: str) -> dict[str, Any]:
    """验签 + 校验 ``aud``，返回 payload。

    ``alg`` 决定用哪把密钥与哪套白名单（见模块 docstring）；HS256 仅在
    ``jwt_hs_fallback`` 打开时放行。异常沿用 PyJWT 原生类型（``PyJWTError`` 家族），
    调用方既有的 ``except jwt.*`` 分支无需改动。
    """
    alg = jwt.get_unverified_header(token).get("alg")
    if alg == RS256:
        key = public_key()
        if key is None:
            raise jwt.InvalidAlgorithmError(
                "RS256 token presented but no public key configured"
            )
        return jwt.decode(token, key, algorithms=[RS256], audience=audience)
    if alg == HS256 and settings.jwt_hs_fallback:
        return jwt.decode(
            token, reveal(settings.jwt_secret), algorithms=[HS256], audience=audience
        )
    raise jwt.InvalidAlgorithmError(f"unsupported or disabled JWT alg: {alg!r}")


# ─────────────────────── JWKS ───────────────────────


def _thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint：仅 kty/n/e 三个必需成员，键字典序、无空白后取 SHA-256。"""
    core = {"e": jwk["e"], "kty": jwk["kty"], "n": jwk["n"]}
    canonical = json.dumps(core, separators=(",", ":"), sort_keys=True).encode()
    return (
        base64.urlsafe_b64encode(hashlib.sha256(canonical).digest())
        .rstrip(b"=")
        .decode()
    )


def jwks_document() -> dict[str, Any]:
    """JWKS（RFC 7517）：公钥集合。未配置公钥时返回空 keys（端点仍 200，便于探活）。"""
    key = public_key()
    if key is None:
        return {"keys": []}
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key, as_dict=True)
    jwk.pop("key_ops", None)
    jwk.update({"use": "sig", "alg": RS256, "kid": _thumbprint(jwk)})
    return {"keys": [jwk]}
