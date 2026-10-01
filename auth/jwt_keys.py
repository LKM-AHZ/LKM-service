"""JWT 密钥与签发/验签原语（RS256-only + JWKS）。

蓝图 §4.2 定案「RS256 非对称」：AUTH 用私钥签发，主服务与网关用公钥验签，使验签方
不再持有可签发的密钥。本仓已**移除 HS256 对称兼容**——不存在按密钥有无在 HS/RS 之间
静默切换的降级路径。

**密钥来源**：``LKM_JWT_PRIVATE_KEY`` / ``LKM_JWT_PUBLIC_KEY``（PEM，经 Infisical/Secret
注入）。只给公钥即「只验不签」的验签方部署。**本地来源为空时**，验签方可让进程从 AUTH 的
``/.well-known/jwks.json`` **运行期拉取并缓存**公钥（蓝图 §2 第 2 条，见
:func:`refresh_public_key_from_jwks` / :func:`verification_status`），从而不必带密钥文件上线。

**算法绑定**：只接受 token 头部的 ``alg == RS256``，其余一律拒——不做「同一把密钥按算法
列表逐个试」，避免 alg confusion（用公钥当 HMAC 密钥的经典混淆）。
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.config import settings
from core.secrets import reveal

logger = logging.getLogger("lkm.auth.jwt_keys")

RS256 = "RS256"

GATEWAY_KEY = "lkm"


def _pem(value: Any, path: str = "") -> str | None:
    """取 PEM 文本：内联（SecretStr）优先，其次读 ``path`` 指向的文件；都没有则 ``None``。

    文件形态是为 k8s Secret 卷挂载与 compose 只读挂载准备的——多行 PEM 塞进 env 变量
    在 env_file / Secret 两侧都容易出转义问题。读不到文件直接抛（属部署配置错误，
    静默降级成「无密钥」会让签发全线失败，比启动期报错更晚暴露）。
    """
    if value is not None:
        text = reveal(value).strip()
        if text:
            return text
        if not path:
            # 显式配了却解析为空（Secret/env 注入成空串——常见于密钥卷没挂上）：
            # 静默返回 None 会让本进程「以为配了密钥其实没有」——签发侧 500、
            # 验签侧全线 401；比启动期直接失败危险得多。
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


def _local_public_pem() -> str | None:
    """本地可得的公钥 PEM：显式公钥优先，其次私钥（可推导公钥）；都没有则 ``None``。"""
    pem = _pem(settings.jwt_public_key, settings.jwt_public_key_file)
    if pem is not None:
        return pem
    return _pem(settings.jwt_private_key, settings.jwt_private_key_file)


def public_key() -> rsa.RSAPublicKey | None:
    """验签公钥：显式配置的 ``jwt_public_key`` → 私钥推导 → **运行期从 JWKS 拉到的**；都无则 ``None``。

    第三档见 :func:`refresh_public_key_from_jwks`（蓝图 §2 第 2 条：本地没有时可从 AUTH 拉取并缓存）。
    """
    pem = _pem(settings.jwt_public_key, settings.jwt_public_key_file)
    if pem is not None:
        return _load_public(pem)
    private_pem = _pem(settings.jwt_private_key, settings.jwt_private_key_file)
    if private_pem is not None:
        return _load_private(private_pem).public_key()
    if _fetched_public_pem is not None:
        return _load_public(_fetched_public_pem)
    return None


# ─────────────────────── 签发 / 验签 ───────────────────────


def encode(payload: dict[str, Any]) -> str:
    """用 RSA 私钥以 RS256 签发；未配私钥即抛（签发方必须有私钥）。"""
    private_pem = _pem(settings.jwt_private_key, settings.jwt_private_key_file)
    # 不用 assert：断言会在 python -O 下被剥掉，退化成 _load_private(None) 的
    # AttributeError，报错点偏离真正的配置缺失。
    if private_pem is None:
        raise RuntimeError(
            "JWT 签发需要 RSA 私钥，但未配置（LKM_JWT_PRIVATE_KEY/_FILE）"
        )
    return jwt.encode(payload, _load_private(private_pem), algorithm=RS256)


def decode(token: str, *, audience: str) -> dict[str, Any]:
    """验签 + 校验 ``aud``，返回 payload。

    只接受 ``alg == RS256``，其余（含 HS256）一律拒——不做「按算法列表逐个试」。
    异常沿用 PyJWT 原生类型（``PyJWTError`` 家族），调用方既有的 ``except jwt.*``
    分支无需改动。
    """
    alg = jwt.get_unverified_header(token).get("alg")
    if alg != RS256:
        raise jwt.InvalidAlgorithmError(f"unsupported JWT alg: {alg!r}")
    key = public_key()
    if key is None:
        raise jwt.InvalidAlgorithmError(
            "RS256 token presented but no public key configured"
        )
    return jwt.decode(token, key, algorithms=[RS256], audience=audience)


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



#: 运行期从 JWKS 拉到的公钥 PEM（本地有钥时不会被使用/写入）。
_fetched_public_pem: str | None = None
#: 后台刷新 task（幂等启动，见 start_public_key_refresh）。
_refresh_task: asyncio.Task[None] | None = None
#: 出站 client 工厂（可注入：测试用 httpx.MockTransport 离线驱动）——与
#: ``auth.user_http._client_factory``、health 探针的 ``_*_factory`` 同款缝。
_jwks_client_factory: Any = None


def _build_jwks_client() -> Any:
    """每次拉取一个 client（配置超时）；注入工厂优先（测试离线驱动用）。"""
    if _jwks_client_factory is not None:
        return _jwks_client_factory()
    import httpx  # 惰性：签发侧/本地有钥时不必引入 httpx 的导入开销

    return httpx.AsyncClient(timeout=settings.auth_http_timeout_s)


def _b64u_to_int(value: str) -> int:
    """base64url（无填充）→ int。JWK 的 ``n``/``e`` 都是这种编码。"""
    return int.from_bytes(
        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big"
    )


def _jwk_to_pem(jwk: dict[str, Any]) -> str:
    """RSA JWK（``n``/``e``）→ SubjectPublicKeyInfo PEM 文本。

    自行转换而不引第三方 JWKS 客户端：本仓已依赖 ``cryptography``（RS256 签发/验签都在用），
    这条只有 ~5 行，不值得为此多一个依赖与一条供应链面。
    """
    numbers = rsa.RSAPublicNumbers(
        _b64u_to_int(str(jwk["e"])), _b64u_to_int(str(jwk["n"]))
    )
    pem = numbers.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem.decode("ascii")


def verification_status() -> str:
    """验签能力状态：``"ok"``（公钥可用）或 ``"unavailable"``（拿不到公钥）。

    RS256-only 下公钥是验签的**前置条件**，故拿不到即不可用。readiness 据此**如实上报**
    （§2 第 2 条）。本函数**绝不抛**——它被探针调用，密钥配置异常不能让探针 500
    （那会把「配置错」伪装成「探针坏」）。
    """
    try:
        return "ok" if public_key() is not None else "unavailable"
    except Exception:
        logger.warning("验签状态判定失败", exc_info=True)
        return "unavailable"


async def refresh_public_key_from_jwks() -> bool:
    """从 AUTH ``/.well-known/jwks.json`` 拉公钥并缓存；返回是否**现在**有公钥可用。

    三种返回 True 的情形：①本地已有公钥（不发请求，本地优先）；②拉取成功并解析出 RSA 公钥。
    失败（未配 ``auth_http_url``／网络／非 200／无 RSA 成员／解析异常）一律返回 False 并记日志，
    **不抛**——调用方（后台刷新 task / readiness）按「验签暂不可用」处理。
    """
    global _fetched_public_pem
    try:
        if _local_public_pem() is not None:
            return True
        base = (settings.auth_http_url or "").strip().rstrip("/")
        if not base:
            return False
        url = f"{base}/.well-known/jwks.json"
        async with _build_jwks_client() as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("JWKS 拉取失败 http=%s", resp.status_code)
            return False
        body = resp.json()
        keys = body.get("keys") if isinstance(body, dict) else None
        for jwk in keys or []:
            if isinstance(jwk, dict) and jwk.get("kty") == "RSA":
                _fetched_public_pem = _jwk_to_pem(jwk)
                return True
        logger.warning("JWKS 未含 RSA 公钥（keys=%d）", len(keys or []))
        return False
    except Exception:
        logger.warning("JWKS 拉取异常", exc_info=True)
        return False


async def _refresh_loop() -> None:
    """周期性尝试拉取公钥（本地有钥时 ``refresh_public_key_from_jwks`` 立即返回，近乎空转）。"""
    interval = max(1, settings.jwks_refresh_s)
    while True:
        await refresh_public_key_from_jwks()
        await asyncio.sleep(interval)


async def start_public_key_refresh() -> None:
    """启动后台刷新 task（幂等）。app 侧经 ``auth.seams.start_verify_key_refresh`` 调用。

    与 lifespan 绑定（§2 第 1 条「启动不阻塞」）：本函数只 create_task，不 await 任何网络调用。
    """
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _refresh_task = asyncio.create_task(_refresh_loop())


async def stop_public_key_refresh() -> None:
    """收尾刷新 task（取消防抖：task 在 sleep 中即被取消）。"""
    global _refresh_task
    task, _refresh_task = _refresh_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # 非取消类异常不得冒泡打断 lifespan 收尾（同 app.main 的收尾纪律）
        logger.warning("公钥刷新 task 收尾异常", exc_info=True)
