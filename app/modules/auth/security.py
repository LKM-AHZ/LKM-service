import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import struct
import time
from typing import Any
from urllib.parse import quote

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_ph = PasswordHasher()
# 虚拟哈希，防枚举
_DUMMY_HASH = _ph.hash("dummy-timing-equalizer")


async def hashpwd(raw: str) -> str:
    """返回 Argon2id 密码哈希（argon2-cffi 默认参数）。

    argon2 是 CPU/内存密集操作，经 asyncio.to_thread 下放到线程池，
    避免在事件循环内同步执行阻塞所有并发请求。
    """
    return await asyncio.to_thread(_ph.hash, raw)


async def verifypwd(raw: str, stored: str) -> bool:
    """验证密码。哈希格式无效或密码不匹配时返回 False，不抛异常。"""

    def _verify() -> bool:
        try:
            _ph.verify(stored, raw)
            return True
        except (VerificationError, ValueError):
            return False

    return await asyncio.to_thread(_verify)


async def dummy_verify() -> None:
    """执行一次与真实验证等成本的虚拟验证，保持时序一致。"""
    await verifypwd("dummy", _DUMMY_HASH)


_ACCESS_TYPE = "access"
_TEMP_TYPE = "temp"

# JWT audience：区分三套互不混用的令牌，防止 token 被误喂给其他端点
_AUD_WEB = "lkm:web"  # 前台 Bearer access
_AUD_TEMP = "lkm:temp"  # 一次性 temp（2FA/recovery/setup）
_AUD_ADMIN = "lkm:admin"  # 后台 access cookie


def create_access_token(
    user_id: int,
    account_level: str,
    role: str,
    trust_device: bool = False,
    token_version: int = 0,
    mfa_verified: bool = False,
    mfa_at: int | None = None,
) -> str:
    now = int(time.time())
    verified_at = mfa_at if mfa_at is not None else now
    payload: dict[str, Any] = {
        "user_id": user_id,
        "account_level": account_level,
        "role": role,
        "trust_device": trust_device,
        "type": _ACCESS_TYPE,
        "token_version": token_version,
        # 危险操作 step-up 2FA 标记 + 信任时刻（epoch 秒）：防前台删除等危险端点被未二次验证的会话滥用。
        # 1 小时窗口由 auth/deps.get_current_user_2fa 校验；刷新轮换经 refresh_tokens.mfa_at 继承原点，窗口不重置。
        "mfa": mfa_verified,
        "mfa_at": verified_at if mfa_verified else None,
        "aud": _AUD_WEB,
        "iat": now,
        "exp": now + settings.access_token_expire_minutes * 60,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    # 先读 payload（不验 aud）判断 token 类型：
    # 若本身不是 access token（例如 temp token），直接抛明确类型错误，而不是被
    # audience 校验提前拦截成 InvalidAudienceError（那样类型检查永远是死代码）。
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"verify_aud": False},
    )
    if payload.get("type") != _ACCESS_TYPE:
        raise ValueError("non-access token")
    # 类型匹配后再严格校验 audience（lkm:web）。
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=_AUD_WEB,
    )


_TEMP_EXPIRE_SECONDS = 60


def create_temp_token(
    user_id: int, purpose: str = "2fa", txn_id: str | None = None
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "user_id": user_id,
        "type": _TEMP_TYPE,
        "purpose": purpose,
        "aud": _AUD_TEMP,
        "iat": now,
        "exp": now + _TEMP_EXPIRE_SECONDS,
    }
    if txn_id:
        payload["txn_id"] = txn_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_temp_token(token: str) -> dict[str, Any]:
    # 先读 payload（不验 aud）判断 token 类型；非 temp token 抛明确类型错误，
    # 避免被 audience 校验提前拦成 InvalidAudienceError 掩盖类型检查。
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"verify_aud": False},
    )
    if payload.get("type") != _TEMP_TYPE:
        raise ValueError("non-temp token")
    # 类型匹配后再严格校验 audience（lkm:temp）。
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=_AUD_TEMP,
    )


_TOTP_DIGITS = 6
_TOTP_STEP = 30


def generate_totp_secret() -> str:
    raw = os.urandom(20)
    return base64.b32encode(raw).decode("ascii")


def get_totp_uri(secret: str, username: str, issuer: str) -> str:
    label = quote(f"{issuer}:{username}")
    params = f"secret={quote(secret)}&issuer={quote(issuer)}&algorithm=SHA1&digits={_TOTP_DIGITS}&period={_TOTP_STEP}"
    return f"otpauth://totp/{label}?{params}"


def _totp_now() -> int:
    return int(time.time()) // _TOTP_STEP


def _totp_code(key: bytes, counter: int) -> str:
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    raw = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{raw % 1_000_000:0{_TOTP_DIGITS}d}"


def verify_totp(secret: str, code: str, window: int = 1) -> int | None:
    try:
        key = base64.b32decode(secret, casefold=True)
    except (binascii.Error, ValueError):
        return None
    now = _totp_now()
    for step in range(now - window, now + window + 1):
        if _totp_code(key, step) == code:
            return step
    return None


_RECOVERY_CODE_BYTES = 10  # 20 hex chars


def generate_recovery_codes(n: int = 10) -> list[tuple[str, str]]:
    codes: list[tuple[str, str]] = []
    for _ in range(n):
        plain = secrets.token_hex(_RECOVERY_CODE_BYTES)
        hashed = hashlib.sha256(plain.encode()).hexdigest()
        codes.append((plain, hashed))
    return codes


def _derive_key() -> bytes:
    """32-byte AES-256 key from SHA-256."""
    return hashlib.sha256(settings.totp_encryption_key.encode()).digest()


def encrypt_secret(plain: str) -> str:
    key = _derive_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plain.encode(), None)
    # store nonce || ciphertext
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_secret(cipher: str) -> str:
    """base64-encoded AES-GCM"""
    key = _derive_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(cipher)
    nonce = raw[:12]
    ct = raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
