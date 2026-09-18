"""双因素认证（TOTP）服务。"""

import hashlib
import uuid
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.err import BizError
from app.db.repo import consume_once, get_or_raise, isolated_update
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import TOTP, RecoveryCode, TempTokenUsage, User
from app.modules.auth.security import (
    decode_temp_token,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_totp_secret,
    get_totp_uri,
    hash_recovery_code,
    legacy_hash_recovery_code,
    verify_totp,
)
from app.modules.auth.service_auth import issue_session_tokens, log_audit

_TOTP_MAX_FAILED = 3
_RECOVERY_MAX_FAILED = 3  # 恢复码暴力尝试上限（对齐 TOTP 的失败锁定）


async def get_enabled_totp(db: AsyncSession, user_id: uuid.UUID) -> TOTP | None:
    """取用户**已启用**的 TOTP 记录，供各处判断"是否开启 2FA"复用。"""
    return (
        (
            await db.execute(
                select(TOTP).where(TOTP.user_id == user_id, TOTP.enabled.is_(True))
            )
        )
        .scalars()
        .first()
    )


async def get_totp(db: AsyncSession, user_id: uuid.UUID) -> TOTP | None:
    """取用户 TOTP 记录（不分启用/禁用），setup/verify/disable 复用，避免重复裸查询。"""
    return (
        (await db.execute(select(TOTP).where(TOTP.user_id == user_id)))
        .scalars()
        .first()
    )


def _check_totp_failed(totp_record: TOTP | None) -> None:
    if totp_record and totp_record.failed_attempts >= _TOTP_MAX_FAILED:
        raise BizError(
            AuthErr.TOTP_CODE_INVALID, "TOTP verification locked – too many failures"
        )


async def _record_totp_failure(db: AsyncSession, totp_record: TOTP | None) -> None:
    """通过子事务（保存点）递增 TOTP 失败计数器，使其在调用方事务因 BizError 回滚时仍能保留。"""
    if not totp_record:
        return

    await isolated_update(
        db,
        sa_update(TOTP)
        .where(TOTP.user_id == totp_record.user_id)
        .values(failed_attempts=TOTP.failed_attempts + 1),
    )
    await db.refresh(totp_record)


async def _reset_totp_failures(db: AsyncSession, totp_record: TOTP | None) -> None:
    """通过调用方会话重置 TOTP 失败计数 —— 仅在成功路径中调用。"""
    if totp_record and totp_record.failed_attempts > 0:
        totp_record.failed_attempts = 0
        await db.flush()


async def _verify_totp_guarded(
    db: AsyncSession, totp_record: TOTP, code: str
) -> int | None:
    """校验 TOTP 并管理失败计数。

    失败次数超限抛 TOTP_CODE_INVALID；校验失败记录一次并抛 TOTP_CODE_INVALID；
    成功清零失败计数并返回匹配的计数器（供重放保护）。
    """
    _check_totp_failed(totp_record)

    plain_secret = decrypt_secret(str(totp_record.secret))
    counter = verify_totp(plain_secret, code)
    if counter is None:
        await _record_totp_failure(db, totp_record)
        raise BizError(AuthErr.TOTP_CODE_INVALID)
    await _reset_totp_failures(db, totp_record)
    return counter


def _recovery_candidate_hashes(plain: str) -> list[str]:
    """恢复码匹配候选哈希：新格式（HMAC+pepper）+ 旧格式（裸 SHA-256）
    兜底，兼容已落库的存量恢复码；新生成的一律走 HMAC。
    """
    hashes = [hash_recovery_code(plain)]
    legacy = legacy_hash_recovery_code(plain)
    if legacy != hashes[0]:
        hashes.append(legacy)
    return hashes


async def _check_recovery_locked(db: AsyncSession, user_id: uuid.UUID) -> None:
    """恢复码暴力尝试超限（任一未用码 failed_attempts 达上限）即拒绝验证。"""
    maxf = await db.scalar(
        select(func.max(RecoveryCode.failed_attempts)).where(
            RecoveryCode.user_id == user_id, RecoveryCode.used.is_(False)
        )
    )
    if (maxf or 0) >= _RECOVERY_MAX_FAILED:
        raise BizError(
            AuthErr.RECOVERY_CODE_INVALID,
            "Recovery verification locked – too many failures",
        )


async def _record_recovery_failure(db: AsyncSession, user_id: uuid.UUID) -> None:
    """恢复码验证失败：经保存点原子递增失败计数，即使外层事务回滚也保留。"""
    await isolated_update(
        db,
        sa_update(RecoveryCode)
        .where(RecoveryCode.user_id == user_id, RecoveryCode.used.is_(False))
        .values(failed_attempts=RecoveryCode.failed_attempts + 1),
    )


async def _reset_recovery_failures(db: AsyncSession, user_id: uuid.UUID) -> None:
    """恢复码成功消费后清零失败计数。"""
    await db.execute(
        sa_update(RecoveryCode)
        .where(RecoveryCode.user_id == user_id, RecoveryCode.used.is_(False))
        .values(failed_attempts=0)
    )


async def consume_recovery_code(
    db: AsyncSession, user_id: uuid.UUID, recovery_code: str
) -> None:
    """原子消费恢复码（一次性）并带失败锁定；失败抛 RECOVERY_CODE_INVALID。

    候选哈希含新旧两种格式，兼容既有存量码与测试种子；失败累计计数并在达上限后锁定。
    """
    if not recovery_code:
        raise BizError(AuthErr.RECOVERY_CODE_INVALID)
    await _check_recovery_locked(db, user_id)
    consumed = await consume_once(
        db,
        RecoveryCode,
        {"used": True, "failed_attempts": 0},
        RecoveryCode.user_id == user_id,
        RecoveryCode.code_hash.in_(_recovery_candidate_hashes(recovery_code)),
        RecoveryCode.used.is_(False),
    )
    if not consumed:
        await _record_recovery_failure(db, user_id)
        raise BizError(AuthErr.RECOVERY_CODE_INVALID)


def _decode_temp_token(raw_token: str) -> dict[str, Any]:
    """解码并验证临时令牌 JWT，但不消费它。"""
    try:
        # 走 security.decode_temp_token：统一 audience 校验（lkm:temp）与 type 检查
        return decode_temp_token(raw_token)
    except Exception as exc:
        raise BizError(AuthErr.TOKEN_INVALID) from exc


async def _check_and_consume_temp_token(
    db: AsyncSession, raw_token: str, user_id: uuid.UUID, txn_id: str | None = None
) -> dict[str, Any]:
    """在成功的第二因素验证后原子地消费临时令牌。"""
    payload = _decode_temp_token(raw_token)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    purpose = payload.get("purpose", "2fa")

    # 使用保存点来隔离插入尝试
    sp = await db.begin_nested()
    try:
        usage = TempTokenUsage(
            token_hash=token_hash,
            user_id=user_id,
            purpose=purpose,
            txn_id=txn_id,
            consumed=True,
        )
        db.add(usage)
        await db.flush()
        await sp.commit()
    except IntegrityError:
        await sp.rollback()
        # 其他人已认领这个哈希值 —— 检查是否已消费
        existing = (
            (
                await db.execute(
                    select(TempTokenUsage).where(
                        TempTokenUsage.token_hash == token_hash
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing and existing.consumed:
            raise BizError(AuthErr.TOKEN_INVALID, "Temp token already used") from None
        raise BizError(AuthErr.TOKEN_INVALID, "Temp token conflict") from None

    return payload


async def _create_auth_tokens(
    db: AsyncSession, user: User, trust_device: bool = False
) -> dict[str, Any]:
    """为给定用户发放访问令牌和刷新令牌。"""
    access_token, raw_refresh = await issue_session_tokens(
        db,
        user,
        trust_device=trust_device,
        mfa_verified=True,
    )
    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "user_id": user.id,
        "account_level": user.account_level,
    }


async def setup_2fa_begin(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.id == user_id,
        options=(selectinload(User.profile),),
    )
    if user.account_level == "local":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    totp_record = await get_totp(db, user_id)
    if totp_record and totp_record.enabled:
        raise BizError(AuthErr.TOTP_ALREADY_ENABLED)

    secret = generate_totp_secret()
    encrypted = encrypt_secret(secret)

    if totp_record:
        totp_record.secret = encrypted
        totp_record.enabled = False
        totp_record.confirmed_saved = False
        totp_record.failed_attempts = 0
    else:
        totp_record = TOTP(user_id=user_id, secret=encrypted, enabled=False)
        db.add(totp_record)
    await db.flush()

    return {
        "secret": secret,
        "qr_code_uri": get_totp_uri(secret, user.username, settings.app_name),
    }


async def setup_2fa_complete(
    db: AsyncSession, user_id: uuid.UUID, code: str
) -> dict[str, Any]:
    totp_record = await get_totp(db, user_id)
    if not totp_record or totp_record.enabled:
        raise BizError(AuthErr.TOTP_NOT_ENABLED)

    await _verify_totp_guarded(db, totp_record, code)

    totp_record.enabled = True
    totp_record.confirmed_saved = False
    await db.flush()

    plain_codes: list[str] = []
    for plain, hashed in generate_recovery_codes(10):
        plain_codes.append(plain)
        rc = RecoveryCode(user_id=user_id, code_hash=hashed, used=False)
        db.add(rc)
    await db.flush()

    await log_audit(db, user_id, "2fa_enabled", "success")

    return {"recovery_codes": plain_codes, "confirmed_saved_required": True}


async def confirm_recovery_codes_saved(
    db: AsyncSession, user_id: uuid.UUID
) -> dict[str, Any]:
    """标记用户已保存其恢复码。"""
    totp_record = await get_totp(db, user_id)
    if not totp_record or not totp_record.enabled:
        raise BizError(AuthErr.TOTP_NOT_ENABLED)
    totp_record.confirmed_saved = True
    await db.flush()
    await log_audit(db, user_id, "recovery_codes_confirmed", "success")
    return {"message": "Recovery codes confirmed saved"}


async def verify_2fa(
    db: AsyncSession,
    temp_token: str,
    code: str | None = None,
    recovery_code: str | None = None,
    trust_device: bool = False,
) -> dict[str, Any]:
    # 仅解码 —— 不消费。消费在成功的第二因素验证*之后*进行，
    # 错误的TOTP/恢复码不会永久地消耗临时令牌或满足恢复检查。
    payload = _decode_temp_token(raw_token=temp_token)
    user_id = payload["user_id"]
    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.id == user_id,
        options=(selectinload(User.profile),),
    )

    if str(user.account_level) == "admin":
        trust_device = False

    txn_id = payload.get("txn_id")

    if recovery_code:
        await consume_recovery_code(db, user_id, recovery_code)
    elif code:
        totp_record = await get_enabled_totp(db, user_id)
        if not totp_record:
            raise BizError(AuthErr.TOTP_NOT_ENABLED)

        actual_counter = await _verify_totp_guarded(db, totp_record, code)

        # 重放保护：原子地存储匹配的计数器
        if not await consume_once(
            db,
            TOTP,
            {"last_counter": actual_counter},
            TOTP.user_id == user_id,
            or_(TOTP.last_counter.is_(None), TOTP.last_counter < actual_counter),
        ):
            raise BizError(AuthErr.TOTP_CODE_INVALID, "TOTP code already used")
    else:
        raise BizError(AuthErr.TOTP_CODE_INVALID)

    # 成功 —— 现在原子地消费临时令牌
    await _check_and_consume_temp_token(db, temp_token, user_id, txn_id=txn_id)

    purpose = payload.get("purpose", "2fa")

    # 临时令牌用途的严格白名单
    # 只有 "2fa" 可以发放登录会话。"recovery" 仅授予第二因素证明。
    # 任何其他用途都会被拒绝。
    _ALLOWED_PURPOSES = {"2fa", "recovery"}
    if purpose not in _ALLOWED_PURPOSES:
        raise BizError(
            AuthErr.TOKEN_INVALID,
            f"Temp token purpose '{purpose}' not allowed for 2FA verification",
        )

    if purpose == "recovery":
        return {
            "access_token": None,
            "refresh_token": None,
            "user_id": user.id,
            "account_level": user.account_level,
            "trust_device": False,
            "mfa_verified": True,
            "message": "2FA verified for recovery",
        }

    # purpose == "2fa" —— 发放登录会话
    result = await _create_auth_tokens(db, user, trust_device=trust_device)
    result["trust_device"] = trust_device
    return result


async def disable_2fa(
    db: AsyncSession,
    user_id: uuid.UUID,
    code: str | None = None,
    recovery_code: str | None = None,
) -> dict[str, Any]:
    totp_record = await get_totp(db, user_id)
    if not totp_record or not totp_record.enabled:
        raise BizError(AuthErr.TOTP_NOT_ENABLED)

    await verify_second_factor(db, user_id, code=code, recovery_code=recovery_code)

    totp_record.enabled = False
    totp_record.secret = ""
    totp_record.confirmed_saved = False
    totp_record.failed_attempts = 0
    totp_record.last_counter = None
    await db.flush()

    await db.execute(sa_delete(RecoveryCode).where(RecoveryCode.user_id == user_id))

    level = await db.scalar(select(User.account_level).where(User.id == user_id))
    await log_audit(db, user_id, "2fa_disabled", "success")

    if level == "admin":
        user = (
            (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        )
        if user:
            user.account_level = "normal"
            await db.flush()
            await log_audit(
                db, user_id, "level_change", "admin -> normal (2FA disabled)"
            )

    return {"message": "2FA disabled"}


async def verify_second_factor(
    db: AsyncSession,
    user_id: uuid.UUID,
    code: str | None = None,
    recovery_code: str | None = None,
) -> None:
    """校验已登录用户的第二因素：TOTP 动态码或恢复码（二选一）。

    - recovery_code 提供 → 原子消费对应恢复码（一次性），不再校验 TOTP；失败抛 RECOVERY_CODE_INVALID。
    - code 提供 → 走 TOTP 校验（含失败计数/重放保护），失败抛 TOTP_CODE_INVALID。
    - 两者都不提供 → 抛 TOTP_CODE_INVALID。
    供危险操作 step-up、关闭 2FA、解绑绑定等「所有 2FA 场景」复用，恢复码作 TOTP 兜底。
    """
    if recovery_code:
        await consume_recovery_code(db, user_id, recovery_code)
        return

    if not code:
        raise BizError(AuthErr.TOTP_CODE_INVALID, "Missing verification code")

    totp_record = await get_enabled_totp(db, user_id)
    if not totp_record:
        raise BizError(AuthErr.TOTP_NOT_ENABLED)

    await _verify_totp_guarded(db, totp_record, code)


async def verify_user_totp(db: AsyncSession, user_id: uuid.UUID, code: str) -> None:
    """校验已登录用户的 TOTP 码（不改状态、不消费，仅二次确认）。失败抛 TOTP_CODE_INVALID。

    兼容旧调用（如既有 step-up/unbind 直接传 TOTP 码）。需恢复码兜底时请用 verify_second_factor。
    """
    totp_record = await get_enabled_totp(db, user_id)
    if not totp_record:
        raise BizError(AuthErr.TOTP_NOT_ENABLED)

    await _verify_totp_guarded(db, totp_record, code)
