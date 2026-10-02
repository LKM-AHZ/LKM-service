import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import struct
import time
import uuid
from typing import Any
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from auth import jwt_keys
from core.config import settings
from core.secrets import reveal

_ph = PasswordHasher()
PASSWORD_MAX_LENGTH = 1024
# 虚拟哈希，防枚举
_DUMMY_HASH = _ph.hash("dummy-timing-equalizer")


async def hashpwd(raw: str) -> str:
    """返回 Argon2id 密码哈希（argon2-cffi 默认参数）。

    argon2 是 CPU/内存密集操作，经 asyncio.to_thread 下放到线程池，
    避免在事件循环内同步执行阻塞所有并发请求。
    """
    if len(raw) > PASSWORD_MAX_LENGTH:
        raise ValueError("Password is too long")
    return await asyncio.to_thread(_ph.hash, raw)


async def verifypwd(raw: str, stored: str) -> bool:
    """验证密码。哈希格式无效或密码不匹配时返回 False，不抛异常。"""
    if len(raw) > PASSWORD_MAX_LENGTH:
        return False

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

_AUD_WEB = "lkm:web"  # 前台 Bearer access
_AUD_TEMP = "lkm:temp"  # 一次性 temp（2FA/recovery/setup）


def create_access_token(
    user_id: uuid.UUID,
    account_level: str,
    role: str,
    trust_device: bool = False,
    token_version: int = 0,
    mfa_verified: bool = False,
    mfa_at: int | None = None,
    active_roles: tuple[str, ...] | None = None,
    session_expires_at: int | None = None,
) -> str:
    now = int(time.time())
    verified_at = mfa_at if mfa_at is not None else now
    access_expires_at = now + settings.access_token_expire_minutes * 60
    if session_expires_at is not None:
        access_expires_at = min(access_expires_at, session_expires_at)
    payload: dict[str, Any] = {
        "user_id": str(user_id),
        "account_level": account_level,
        "role": role,
        "trust_device": trust_device,
        "type": _ACCESS_TYPE,
        "token_version": token_version,
        # 单 token 标识：登出时按 jti 写 L2 黑名单（auth.token_revocation），验签后先查黑名单
        # 再回查 DB。token_version 是「踢该用户全部会话」的全局判据，jti 只废这一枚 token
        # ——两者分工，jti 只加拒、不取代 DB 权威判据。
        "jti": uuid.uuid4().hex,
        # 危险操作 step-up 2FA 标记 + 信任时刻（epoch 秒）：防前台删除等危险端点被未二次验证的会话滥用。
        # 1 小时窗口由 auth/deps.get_current_user_2fa 校验；刷新轮换经 refresh_tokens.mfa_at 继承原点，窗口不重置。
        "mfa": mfa_verified,
        "mfa_at": verified_at if mfa_verified else None,
        "aud": _AUD_WEB,
        # APISIX jwt-auth 靠该 claim 查消费者（见 jwt_keys.GATEWAY_KEY）：
        "key": jwt_keys.GATEWAY_KEY,
        "iat": now,
        "exp": access_expires_at,
    }
    if active_roles is not None:
        payload["active_roles"] = list(active_roles)
    return jwt_keys.encode(payload)


def decode_access_token(token: str) -> dict[str, Any]:
    # 单次验签：同时校验 audience(lkm:web) 与类型标记(access)。
    # 之前先读"不验 aud"看类型、再"验 aud"验第二遍，导致每次调用重复验签(HMAC)两次。
    payload = jwt_keys.decode(token, audience=_AUD_WEB)
    if payload.get("type") != _ACCESS_TYPE:
        raise ValueError("non-access token")
    return payload


_TEMP_EXPIRE_SECONDS = 60


def create_temp_token(
    user_id: uuid.UUID, purpose: str = "2fa", txn_id: str | None = None
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "user_id": str(user_id),
        "type": _TEMP_TYPE,
        "purpose": purpose,
        "aud": _AUD_TEMP,
        # APISIX jwt-auth 靠该 claim 查消费者（见 jwt_keys.GATEWAY_KEY）：
        "key": jwt_keys.GATEWAY_KEY,
        "iat": now,
        "exp": now + _TEMP_EXPIRE_SECONDS,
    }
    if txn_id:
        payload["txn_id"] = txn_id
    return jwt_keys.encode(payload)


def decode_temp_token(token: str) -> dict[str, Any]:
    # 单次验签：同时校验 audience(lkm:temp) 与类型标记(temp)。
    payload = jwt_keys.decode(token, audience=_AUD_TEMP)
    if payload.get("type") != _TEMP_TYPE:
        raise ValueError("non-temp token")
    return payload


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
        if hmac.compare_digest(_totp_code(key, step), code):
            return step
    return None


_RECOVERY_CODE_BYTES = 10  # 20 hex chars


def hash_recovery_code(plain: str) -> str:
    """恢复码 HMAC 存储：带 pepper 的 HMAC-SHA256（非裸 SHA-256）。

    裸哈希在明文空间可离线枚举（恢复码熵 80-bit 虽低，但带 pepper 可挡离线彩虹表/暴力），
    与验证码哈希一致（见 service_verify.hash_code）。
    """
    pepper = reveal(settings.verification_code_pepper).encode()
    return hmac.new(pepper, plain.encode(), hashlib.sha256).hexdigest()


def legacy_hash_recovery_code(plain: str) -> str:
    """旧版恢复码哈希（裸 SHA-256）——仅用于校验既有存量，新码一律走 hash_recovery_code。"""
    return hashlib.sha256(plain.encode()).hexdigest()


def generate_recovery_codes(n: int = 10) -> list[tuple[str, str]]:
    codes: list[tuple[str, str]] = []
    for _ in range(n):
        plain = secrets.token_hex(_RECOVERY_CODE_BYTES)
        hashed = hash_recovery_code(plain)
        codes.append((plain, hashed))
    return codes


_CIPHER_V2_PREFIX = "v2:"
_HKDF_INFO = b"lkm:totp-secret:v2"  # 域分离：防同主密钥在别处派生出同字节
_SALT_LEN = 16
_NONCE_LEN = 12
_GCM_TAG_LEN = 16


def _hkdf_key(salt: bytes) -> bytes:
    """HKDF-SHA256：主密钥 + 每记录盐 → 32B AES 密钥。"""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    ).derive(reveal(settings.totp_encryption_key).encode())


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("malformed ciphertext") from err


def _gcm_decrypt(key: bytes, nonce: bytes, ct: bytes) -> str:
    try:
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as err:
        raise ValueError("malformed ciphertext") from err


def encrypt_secret(plain: str) -> str:
    """AES-256-GCM 加密：``v2:`` + base64(salt(16) || nonce(12) || ct||tag)，每次随机盐 + nonce。"""
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_hkdf_key(salt)).encrypt(nonce, plain.encode(), None)
    return _CIPHER_V2_PREFIX + base64.b64encode(salt + nonce + ct).decode("ascii")


def decrypt_secret(cipher: str) -> str:
    """
    解密 ``encrypt_secret`` 产物。
    密文损坏/被轮换/被截断/格式不符时统一抛 ``ValueError("malformed ciphertext")``：原来的
    ``base64.b64decode`` 默认丢弃非字母表字符（静默解出错字节）、坏 padding 抛
    binascii.Error、长度不足则在切片后崩、tag 不符抛 cryptography 的 InvalidTag——
    四种形态各异且都不可诊断。收成一个明确的失败，日志里一眼能区分「密文坏了」和代码 bug。
    """
    if not cipher.startswith(_CIPHER_V2_PREFIX):
        raise ValueError("malformed ciphertext")
    raw = _unb64(cipher[len(_CIPHER_V2_PREFIX) :])
    if len(raw) < _SALT_LEN + _NONCE_LEN + _GCM_TAG_LEN:
        raise ValueError("malformed ciphertext")
    salt = raw[:_SALT_LEN]
    nonce = raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    ct = raw[_SALT_LEN + _NONCE_LEN :]
    return _gcm_decrypt(_hkdf_key(salt), nonce, ct)
