"""Passkey（WebAuthn）服务 —— 注册、登录、凭证管理。

基于官方 webauthn（Duwab 2.7.x）顶层函数式 API 实现，取代早期自写 COSE /
authenticatorData / ECDSA 解析。浏览器签名以 raw r||s（64 字节）返回，库要求
DER，故统一经 ``encode_dss_signature`` 转换（见 ``_signature_raw_to_der``）。
"""

import base64
import json
import os
import secrets
import uuid

from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from app.core.config import settings
from app.core.err import BizError
from app.db.base import expires_at, now_iso
from app.db.repo import consume_once, get_or_raise
from app.db.repository import DbSession
from auth.errors import AuthErr
from auth.models import PasskeyChallenge, PasskeyCredential, User
from auth.repository import (
    PasskeyChallengeRepository,
    PasskeyCredentialRepository,
)

_CHALLENGE_TTL_MINUTES = 5


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s)


def _signature_raw_to_der(sig: bytes) -> bytes:
    """浏览器返回的 ECDSA 签名是 raw r||s（64 字节）；库 verify 需 DER 格式。"""
    if len(sig) == 64:
        return encode_dss_signature(
            int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        )
    return sig


async def _store_challenge(db: DbSession) -> tuple[str, bytes]:
    """将 WebAuthn 挑战码持久化到数据库（跨 worker 共享，过期自动失效）。

    返回 (challenge_id, 原始挑战码 bytes)——bytes 传给库生成 options 并在验证时
    作为 expected_challenge 使用；DB 存的是 base64url 文本便于列类型匹配。
    """
    challenge_id = secrets.token_hex(16)
    challenge = os.urandom(32)
    expiry = expires_at(minutes=_CHALLENGE_TTL_MINUTES)
    await PasskeyChallengeRepository(db).create(
        challenge_id=challenge_id,
        challenge=_b64(challenge),
        expires_at=expiry,
    )
    return challenge_id, challenge


async def _consume_challenge(db: DbSession, challenge_id: str) -> bytes:
    """原子地消费挑战码（条件 UPDATE 防止重放），返回原始挑战码 bytes。"""
    now = now_iso()
    if not await consume_once(
        db,
        PasskeyChallenge,
        {"consumed": True},
        *PasskeyChallengeRepository(db).consume_conditions(challenge_id, now),
    ):
        return b""
    row = await PasskeyChallengeRepository(db).get_by_challenge_id(challenge_id)
    return _b64decode(str(row.challenge)) if row else b""


_CLEANUP_INTERVAL_SECONDS = 300  # 5 分钟


async def cleanup_expired_challenges() -> None:
    """后台任务：定期清理已过期或已被消费的挑战码。"""
    import asyncio
    import logging

    from auth.db.session import new_auth_session

    _log = logging.getLogger("passkey.cleanup")

    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            # S5 拆库后 PasskeyChallenge 在 auth 独立库 → 必须用 auth 会话，不能用业务 new_session。
            db = await new_auth_session()
            try:
                now = now_iso()
                deleted = await PasskeyChallengeRepository(
                    db
                ).delete_expired_or_consumed(now)
                await db.commit()
                if deleted:
                    _log.info(
                        "Cleaned up %d expired/consumed passkey challenges", deleted
                    )
            except (OSError, RuntimeError):
                await db.rollback()
                _log.exception("Failed to clean up expired passkey challenges")
            finally:
                await db.close()
        except Exception:
            _log.exception(
                "cleanup_expired_challenges: unexpected error outside DB session"
            )


def _registration_credential(credential: dict) -> dict:
    """补全前端缺失的 WebAuthn 字段：``id``(=rawId) 与 ``type``。

    前端 webauthn.ts 只回 ``rawId`` + ``response``；parse_registration_credential_json
    要求 ``id`` 与 ``rawId`` 等价、``type`` 为 "public-key"，故在此补上。
    """
    raw_id = credential.get("rawId") or ""
    return {
        "id": raw_id,
        "rawId": raw_id,
        "type": "public-key",
        "response": credential.get("response") or {},
    }


def _authentication_credential(credential: dict) -> dict:
    """补全认证凭据字段，并把签名 raw r||s 转为 DER（库 verify 期望 DER）。"""
    raw_id = credential.get("rawId") or ""
    response = dict(credential.get("response") or {})
    signature_b64 = response.get("signature")
    if signature_b64:
        response["signature"] = _b64(
            _signature_raw_to_der(_b64decode(str(signature_b64)))
        )
    return {
        "id": raw_id,
        "rawId": raw_id,
        "type": "public-key",
        "response": response,
    }


async def begin_passkey_registration(db: DbSession, user_id: uuid.UUID) -> dict:
    user = await get_or_raise(db, User, AuthErr.USER_NOT_FOUND, User.id == user_id)

    challenge_id, challenge = await _store_challenge(db)

    existing = await PasskeyCredentialRepository(db).list_for_user(user_id)
    exclude_credentials = [
        PublicKeyCredentialDescriptor(
            id=_b64decode(c.credential_id),
        )
        for c in existing
    ]

    user_id_bytes = user.id.bytes
    options = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_name=user.username,
        user_id=user_id_bytes,
        user_display_name=user.username,
        challenge=challenge,
        timeout=60000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            # 强制本地用户验证（指纹/面容等），保证无密码登录的防冒用强度
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=exclude_credentials,
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )

    return {
        "challenge_id": challenge_id,
        "public_key": json.loads(options_to_json(options)),
    }


async def complete_passkey_registration(
    db: DbSession, user_id: uuid.UUID, credential: dict
) -> dict:
    raw_id = credential.get("rawId") or ""
    challenge_id = credential.get("challenge_id")

    if not raw_id or not challenge_id:
        raise BizError(
            AuthErr.PASSKEY_REGISTRATION_FAILED, "rawId and challenge_id required"
        )

    challenge = await _consume_challenge(db, challenge_id)
    if not challenge:
        raise BizError(
            AuthErr.PASSKEY_REGISTRATION_FAILED, "Challenge expired or invalid"
        )

    try:
        verified = verify_registration_response(
            credential=_registration_credential(credential),
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.origin,
        )
    except WebAuthnException as exc:
        raise BizError(AuthErr.PASSKEY_REGISTRATION_FAILED, str(exc)) from exc

    cred_raw = verified.credential_id
    # credential_id 列语义 = base64url 文本串（异于 raw bytes；登录侧浏览器 rawId 亦为
    # base64url-unpadded 文本）。Duwab 顶层 verify 返回的 credential_id 是原始 bytes，
    # 直接入库/比较会把 PG varchar 列变成 bytea 比较而爆。统一编码。
    cred_id = (
        _b64(cred_raw) if isinstance(cred_raw, (bytes, bytearray)) else str(cred_raw)
    )
    public_key_bytes = verified.credential_public_key

    existing = await PasskeyCredentialRepository(db).find_by_credential_id(cred_id)
    if existing:
        raise BizError(
            AuthErr.PASSKEY_REGISTRATION_FAILED, "Credential already registered"
        )

    device_name = credential.get("device_name") or "Unknown device"
    await PasskeyCredentialRepository(db).create(
        user_id=user_id,
        credential_id=cred_id,
        public_key=_b64(public_key_bytes),
        sign_count=verified.sign_count,
        device_name=device_name,
    )
    return {"message": "Passkey registered successfully", "device_name": device_name}


async def begin_passkey_login(db: DbSession) -> dict:
    challenge_id, challenge = await _store_challenge(db)
    options = generate_authentication_options(
        rp_id=settings.rp_id,
        challenge=challenge,
        timeout=60000,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return {
        "challenge_id": challenge_id,
        "public_key": json.loads(options_to_json(options)),
    }


async def complete_passkey_login(db: DbSession, credential: dict) -> dict:
    raw_id = credential.get("rawId") or ""
    challenge_id = credential.get("challenge_id")

    if not raw_id or not challenge_id:
        raise BizError(
            AuthErr.PASSKEY_VERIFICATION_FAILED, "rawId and challenge_id required"
        )

    challenge = await _consume_challenge(db, challenge_id)
    if not challenge:
        raise BizError(
            AuthErr.PASSKEY_VERIFICATION_FAILED, "Challenge expired or invalid"
        )

    passkey = await get_or_raise(
        db,
        PasskeyCredential,
        AuthErr.PASSKEY_VERIFICATION_FAILED,
        PasskeyCredential.credential_id == raw_id,
        detail="Credential not found",
    )

    try:
        verification = verify_authentication_response(
            credential=_authentication_credential(credential),
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.origin,
            credential_public_key=_b64decode(str(passkey.public_key)),
            credential_current_sign_count=passkey.sign_count,
        )
    except WebAuthnException as exc:
        raise BizError(AuthErr.PASSKEY_VERIFICATION_FAILED, str(exc)) from exc

    await PasskeyCredentialRepository(db).update(
        passkey, sign_count=verification.new_sign_count
    )

    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.id == passkey.user_id
    )

    if user.account_level == "local":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    from auth.service_auth import finalize_auth_response

    return await finalize_auth_response(db, user)


async def list_credentials(db: DbSession, user_id: uuid.UUID) -> list[dict]:
    creds = await PasskeyCredentialRepository(db).list_for_user(user_id)
    return [
        {
            "id": c.id,
            "credential_id": c.credential_id,
            "device_name": c.device_name,
            "created_at": c.created_at,
        }
        for c in creds
    ]


async def delete_credential(
    db: DbSession, user_id: uuid.UUID, credential_id: uuid.UUID
) -> dict:
    cred = await PasskeyCredentialRepository(db).get_for_user_or_raise(
        credential_id,
        user_id,
        AuthErr.PASSKEY_VERIFICATION_FAILED,
        detail="Credential not found",
    )
    await PasskeyCredentialRepository(db).delete(cred)
    return {"message": "Credential deleted"}
