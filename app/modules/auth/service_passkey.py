"""Passkey（WebAuthn）服务 —— 注册、登录、凭证管理。"""

import base64
import hashlib
import json
import os
import secrets
import struct

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.err import BizError, ErrCode
from app.db.models import User, expires_at, now_iso
from app.db.repo import consume_once, get_or_raise
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import PasskeyChallenge, PasskeyCredential

_CHALLENGE_TTL_MINUTES = 5


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s)


async def _store_challenge(db: AsyncSession) -> tuple[str, str]:
    """将 WebAuthn 挑战码持久化到数据库（跨 worker 共享，过期自动失效）。"""
    challenge_id = secrets.token_hex(16)
    challenge = _b64(os.urandom(32))
    expiry = expires_at(minutes=_CHALLENGE_TTL_MINUTES)
    db.add(PasskeyChallenge(
        challenge_id=challenge_id,
        challenge=challenge,
        expires_at=expiry,
    ))
    await db.flush()
    return challenge_id, challenge


async def _consume_challenge(db: AsyncSession, challenge_id: str) -> str:
    """原子地消费挑战码 —— 使用条件 UPDATE 防止重放。"""
    now = now_iso()
    if not await consume_once(
        db,
        PasskeyChallenge,
        {"consumed": True},
        PasskeyChallenge.challenge_id == challenge_id,
        PasskeyChallenge.consumed.is_(False),
        PasskeyChallenge.expires_at > now,
    ):
        return ""
    row = (
        (
            await db.execute(
                select(PasskeyChallenge).where(
                    PasskeyChallenge.challenge_id == challenge_id
                )
            )
        )
        .scalars()
        .first()
    )
    return str(row.challenge) if row else ""


_CLEANUP_INTERVAL_SECONDS = 300  # 5 分钟


async def cleanup_expired_challenges() -> None:
    """后台任务：定期清理已过期或已被消费的挑战码。"""
    import asyncio
    import logging

    from app.db.session import new_session

    _log = logging.getLogger("passkey.cleanup")

    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            db = new_session()
            try:
                from sqlalchemy import delete as sa_delete
                from sqlalchemy import or_
                now = now_iso()
                result = await db.execute(
                    sa_delete(PasskeyChallenge).where(
                        or_(PasskeyChallenge.consumed.is_(True), PasskeyChallenge.expires_at <= now)
                    )
                )
                await db.commit()
                deleted = int(result.rowcount) if result.rowcount else 0  # type: ignore[union-attr]
                if deleted:
                    _log.info("Cleaned up %d expired/consumed passkey challenges", deleted)
            except (OSError, RuntimeError):
                await db.rollback()
                _log.exception("Failed to clean up expired passkey challenges")
            finally:
                db.close()
        except Exception:
            _log.exception("cleanup_expired_challenges: unexpected error outside DB session")


def _parse_client_data(client_data_json_b64: str) -> dict:
    """将 clientDataJSON 解码并解析为 UTF-8 JSON。出现任何错误时抛出 PASSKEY_VERIFICATION_FAILED。"""
    try:
        raw = _b64decode(client_data_json_b64)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Invalid clientDataJSON") from exc


def _parse_authenticator_data(auth_data: bytes) -> dict:
    """按 WebAuthn 规范 §6.1 解析 authenticatorData。"""
    if len(auth_data) < 37:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Authenticator data too short")

    rp_id_hash = auth_data[:32]
    flags = auth_data[32]
    sign_count = struct.unpack(">I", auth_data[33:37])[0]

    pos = 37
    attested_credential_data = None
    if flags & 0x40:  # AT 标志位
        if len(auth_data) < pos + 18:
            raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED)
        aaguid = auth_data[pos : pos + 16]
        pos += 16
        cred_id_len = struct.unpack(">H", auth_data[pos : pos + 2])[0]
        pos += 2
        credential_id = auth_data[pos : pos + cred_id_len]
        pos += cred_id_len
        # 解析 COSE key（简化版：提取原始密钥材料）
        cose_key, consumed = _parse_cose_key(auth_data[pos:])
        pos += consumed
        attested_credential_data = {
            "aaguid": aaguid,
            "credential_id": credential_id,
            "cose_key": cose_key,
        }

    return {
        "rp_id_hash": rp_id_hash,
        "flags": flags,
        "sign_count": sign_count,
        "attested_credential_data": attested_credential_data,
    }


def _parse_cose_key(data: bytes) -> tuple[dict, int]:
    """解析 CBOR 编码的 COSE_Key 结构。"""
    import cbor2

    cose = cbor2.loads(data)
    key_type = cose.get(1)   # kty（密钥类型）
    alg = cose.get(3)         # alg（算法）
    consumed = len(cbor2.dumps(cose))

    if key_type != 2:  # EC2（椭圆曲线）
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, f"Unsupported COSE key type: {key_type}")

    x_bytes = cose.get(-2)  # x 坐标
    y_bytes = cose.get(-3)  # y 坐标
    crv = cose.get(-1)       # 曲线（必须为 1 = P-256）

    if crv != 1:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, f"Unsupported EC curve: {crv}")

    if not x_bytes or not y_bytes:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Missing COSE key coordinates")

    return {"kty": key_type, "alg": alg, "crv": crv, "x": x_bytes, "y": y_bytes}, consumed


def _verify_origin(expected_origin: str, client_data: dict) -> None:
    origin = client_data.get("origin", "")
    if origin != expected_origin:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Origin mismatch")


def _verify_challenge(expected_challenge: str, client_data: dict) -> None:
    challenge = client_data.get("challenge", "")
    if challenge != expected_challenge:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Challenge mismatch")


def _verify_rp_id_hash(auth_data: dict) -> None:
    expected_hash = hashlib.sha256(settings.rp_id.encode()).digest()
    if auth_data["rp_id_hash"] != expected_hash:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "RP ID hash mismatch")


def _verify_user_presence(auth_data: dict) -> None:
    if not (auth_data["flags"] & 0x01):  # UP 标志位（用户在场）
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "User presence not verified")

def _build_signed_data(auth_data_bytes: bytes, client_data_json_b64: str) -> bytes:
    """构建待验证的二进制数据：authenticatorData || SHA-256(clientDataJSON)。"""
    client_data_hash = hashlib.sha256(
        _b64decode(client_data_json_b64)
    ).digest()
    return auth_data_bytes + client_data_hash


def _signature_to_der(raw_sig: bytes) -> bytes:
    if len(raw_sig) != 64:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Invalid signature length")
    r = raw_sig[:32]
    s = raw_sig[32:]

    def _der_int(val: bytes) -> bytes:
        # Strip leading zeros but keep sign bit
        stripped = val.lstrip(b"\x00")
        if not stripped:
            stripped = b"\x00"
        if stripped[0] & 0x80:
            stripped = b"\x00" + stripped
        return b"\x02" + bytes([len(stripped)]) + stripped

    r_der = _der_int(r)
    s_der = _der_int(s)
    inner = r_der + s_der
    return b"\x30" + bytes([len(inner)]) + inner

async def begin_passkey_registration(db: AsyncSession, user_id: int) -> dict:
    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.id == user_id
    )

    challenge_id, challenge = await _store_challenge(db)
    user_handle = user_id.to_bytes(8, "big")

    existing = (
        (
            await db.execute(
                select(PasskeyCredential).where(
                    PasskeyCredential.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    exclude_credentials = [
        {"type": "public-key", "id": _b64(c.credential_id.encode("utf-8"))}
        for c in existing
    ]

    return {
        "challenge_id": challenge_id,
        "public_key": {
            "challenge": challenge,
            "rp": {"id": settings.rp_id, "name": settings.rp_name},
            "user": {
                "id": _b64(user_handle),
                "name": user.username,
                "displayName": user.username,
            },
            "pubKeyCredParams": [
                {"type": "public-key", "alg": -7},
                {"type": "public-key", "alg": -257},
            ],
            "timeout": 60000,
            "excludeCredentials": exclude_credentials,
            "authenticatorSelection": {
                "authenticatorAttachment": "platform",
                "userVerification": "preferred",
            },
            "attestation": "direct",
        },
    }

async def _prep_passkey_credential(db: AsyncSession, credential: dict, err: ErrCode) -> tuple[str, str, dict, str, dict]:
    """从 credential dict 提取并校验基础字段，消费 challenge。返回 (raw_id, challenge, response, client_data_json, client_data)。"""
    raw_id: str = credential.get("rawId")  # type: ignore[assignment]
    challenge_id: str | None = credential.get("challenge_id")
    response: dict = credential.get("response", {})  # type: ignore[assignment]

    if not raw_id or not challenge_id:
        raise BizError(err, "rawId and challenge_id required")

    challenge = await _consume_challenge(db, challenge_id)
    if not challenge:
        raise BizError(err, "Challenge expired or invalid")

    client_data_json_b64: str = response.get("clientDataJSON")  # type: ignore[assignment]
    client_data = _parse_client_data(client_data_json_b64)
    _verify_origin(settings.origin, client_data)
    _verify_challenge(challenge, client_data)

    return raw_id, challenge, response, client_data_json_b64, client_data


async def complete_passkey_registration(
    db: AsyncSession, user_id: int, credential: dict
) -> dict:
    raw_id, _challenge, response, client_data_json_b64, _client_data = await _prep_passkey_credential(
        db, credential, AuthErr.PASSKEY_REGISTRATION_FAILED
    )

    attestation_object_b64 = response.get("attestationObject")

    if not client_data_json_b64 or not attestation_object_b64:
        raise BizError(AuthErr.PASSKEY_REGISTRATION_FAILED, "clientDataJSON and attestationObject required")

    try:
        attestation_bytes = _b64decode(str(attestation_object_b64))
        import cbor2
        att_obj = cbor2.loads(attestation_bytes)
    except Exception as exc:
        raise BizError(AuthErr.PASSKEY_REGISTRATION_FAILED, "Invalid attestationObject") from exc

    fmt = att_obj.get("fmt", "")
    auth_data_bytes = att_obj.get("authData", b"")
    auth_data = _parse_authenticator_data(auth_data_bytes)

    _verify_rp_id_hash(auth_data)
    _verify_user_presence(auth_data)

    ata: dict = auth_data.get("attested_credential_data")  # type: ignore[assignment]
    if not ata:
        raise BizError(AuthErr.PASSKEY_REGISTRATION_FAILED, "No attested credential data")

    if fmt == "packed":
        att_stmt = att_obj.get("attStmt", {})
        sig = att_stmt.get("sig", b"")
        x5c = att_stmt.get("x5c", [])

        if x5c:
            try:
                x5c_0: bytes = x5c[0]  # pyright: ignore[reportAssignmentType]
                cert = x509.load_der_x509_certificate(x5c_0)
                signed_data_part = auth_data_bytes + hashlib.sha256(
                    _b64decode(client_data_json_b64)
                ).digest()
                pubkey = cert.public_key()
                if isinstance(pubkey, ec.EllipticCurvePublicKey):
                    pubkey.verify(sig, signed_data_part, ec.ECDSA(hashes.SHA256()))  # pyright: ignore[reportArgumentType]
            except Exception as exc:
                raise BizError(AuthErr.PASSKEY_REGISTRATION_FAILED, "Attestation signature invalid") from exc

    cose_key = ata["cose_key"]
    x, y = cose_key["x"], cose_key["y"]
    public_key_bytes = b"\x04" + x + y

    existing = (
        (
            await db.execute(
                select(PasskeyCredential).where(
                    PasskeyCredential.credential_id == raw_id
                )
            )
        )
        .scalars()
        .first()
    )
    if existing:
        raise BizError(AuthErr.PASSKEY_REGISTRATION_FAILED, "Credential already registered")

    device_name = credential.get("device_name", "Unknown device")

    cred = PasskeyCredential(
        user_id=user_id,
        credential_id=raw_id, # type: ignore[union-attr]
        public_key=_b64(public_key_bytes),
        sign_count=auth_data["sign_count"],
        device_name=device_name,
    )
    db.add(cred)

    user = (
        (
            await db.execute(select(User).where(User.id == user_id))
        )
        .scalars()
        .first()
    )
    if user and str(user.account_level) == "local":
        await db.flush()
    return {"message": "Passkey registered successfully", "device_name": device_name}

async def begin_passkey_login(db: AsyncSession) -> dict:
    challenge_id, challenge = await _store_challenge(db)
    return {
        "challenge_id": challenge_id,
        "public_key": {
            "challenge": challenge,
            "rpId": settings.rp_id,
            "timeout": 60000,
            "userVerification": "preferred",
        },
    }

async def complete_passkey_login(db: AsyncSession, credential: dict) -> dict:
    raw_id, _challenge, response, client_data_json_b64, _client_data = await _prep_passkey_credential(
        db, credential, AuthErr.PASSKEY_VERIFICATION_FAILED
    )

    authenticator_data_b64 = response.get("authenticatorData")
    signature_b64 = response.get("signature")

    if not authenticator_data_b64 or not signature_b64:
        raise BizError(
            AuthErr.PASSKEY_VERIFICATION_FAILED,
            "authenticatorData and signature required",
        )

    auth_data_bytes = _b64decode(str(authenticator_data_b64))
    auth_data = _parse_authenticator_data(auth_data_bytes)
    _verify_rp_id_hash(auth_data)
    _verify_user_presence(auth_data)

    passkey = await get_or_raise(
        db, PasskeyCredential, AuthErr.PASSKEY_VERIFICATION_FAILED,
        PasskeyCredential.credential_id == raw_id,
        detail="Credential not found",
    )

    public_key_bytes = _b64decode(str(passkey.public_key))
    signed_data = _build_signed_data(auth_data_bytes, client_data_json_b64)
    signature_raw = _b64decode(str(signature_b64))
    signature_der = _signature_to_der(signature_raw)

    try:
        pubkey = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_bytes)
        pubkey.verify(signature_der, signed_data, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, Exception):
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, "Invalid signature") from None

    reported_count = auth_data["sign_count"]
    passkey.sign_count = max(passkey.sign_count, reported_count)
    await db.flush()

    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.id == passkey.user_id
    )

    if user.account_level == "local":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    from app.modules.auth.service_auth import finalize_auth_response
    return await finalize_auth_response(db, user)

async def list_credentials(db: AsyncSession, user_id: int) -> list[dict]:
    creds = (
        (
            await db.execute(
                select(PasskeyCredential).where(
                    PasskeyCredential.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": c.id,
            "credential_id": c.credential_id,
            "device_name": c.device_name,
            "created_at": c.created_at,
        }
        for c in creds
    ]

async def delete_credential(db: AsyncSession, user_id: int, credential_id: int) -> dict:
    cred = await get_or_raise(
        db, PasskeyCredential, AuthErr.PASSKEY_VERIFICATION_FAILED,
        PasskeyCredential.id == credential_id,
        PasskeyCredential.user_id == user_id,
        detail="Credential not found",
    )
    await db.delete(cred)
    await db.flush()
    return {"message": "Credential deleted"}
