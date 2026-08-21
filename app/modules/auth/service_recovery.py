"""密码恢复服务。"""

import hashlib
import secrets
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.db.models import User, expires_at, now_iso
from app.db.repo import consume_once, get_or_raise
from app.modules.auth import security
from app.modules.auth.channels import CHANNELS, channel_for
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import MagicLink, RecoveryTransaction, TempTokenUsage
from app.modules.auth.security import (
    create_temp_token,
    dummy_verify,
    hashpwd,
)
from app.modules.auth.service_2fa import get_enabled_totp
from app.modules.auth.service_auth import (
    BackgroundTasksLike,
    log_audit,
    revoke_all_refresh_tokens,
    verify_magic_link,
)
from app.modules.auth.service_verify import check_code_rate_limit


async def find_user_by_contact(db: AsyncSession, field: str, value: str) -> User:
    """通过邮箱或手机号查找用户。"""
    channel = CHANNELS.get(field)
    if channel is None:
        raise BizError(CommonErr.INVALID_INPUT, "field must be 'email' or 'phone'")
    user = await channel.find_user(db, value)
    if user is None:
        raise BizError(AuthErr.USER_NOT_FOUND)

    if user.account_level == "local":
        raise BizError(
            AuthErr.RECOVERY_NOT_SUPPORTED, "Local accounts do not support recovery"
        )

    if user.account_level == "admin":
        raise BizError(
            AuthErr.RECOVERY_METHOD_UNAVAILABLE,
            "Admin accounts must use the dedicated admin recovery flow",
        )

    return user


async def _user_requires_mfa(db: AsyncSession, user: User) -> bool:
    """如果用户启用了 TOTP 并且必须使用第二因素验证，则返回 True。"""
    if user.account_level == "admin":
        return True
    totp = await get_enabled_totp(db, user.id)
    return totp is not None


async def _reset_password(db: AsyncSession, user: User, new_password: str) -> None:
    """哈希新密码、设置它、解锁账户、撤销所有令牌，并记录审计日志。"""
    user.hashed_password = await hashpwd(new_password)
    user.is_locked = False
    user.locked_until = None
    user.failed_login_attempts = 0
    user.updated_at = now_iso()  # 使已发放的访问令牌失效（iat < updated_at）
    await db.flush()

    await revoke_all_refresh_tokens(db, user.id)

    await log_audit(db, user.id, "password_reset", detail="recovery")


async def check_recovery_methods(_db: AsyncSession, _account: str) -> dict[str, Any]:
    """检查账户可用的恢复方法。"""
    # 始终统一 —— 不泄露账户是否存在、是否为 local 或 admin
    return {"recoverable": False}


async def recover_by_contact(
    db: AsyncSession, contact: str, code: str, new_password: str | None = None
) -> dict[str, Any]:
    """第 1 步：通过邮箱或手机号验证码重置密码（通道由 contact 自动判定）。"""
    channel = channel_for(contact)
    await channel.consume_code(db, contact, code, "reset")
    user = await find_user_by_contact(db, channel.name, contact)

    if await _user_requires_mfa(db, user):
        return await _start_user_recovery_txn(db, user)

    if not new_password:
        raise BizError(CommonErr.INVALID_INPUT, "new_password is required")
    await _reset_password(db, user, new_password)
    return {"message": "Password reset successful"}


async def recover_by_magic_link(
    db: AsyncSession, token: str, new_password: str | None = None
) -> dict[str, Any]:
    """第 1 步：验证密码恢复的魔法链接。"""
    await verify_magic_link(db, token, purpose="reset")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    link_record = await get_or_raise(
        db,
        MagicLink,
        AuthErr.TOKEN_INVALID,
        MagicLink.token_hash == token_hash,
    )

    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.email == link_record.email,
    )

    if user.account_level == "admin":
        raise BizError(
            AuthErr.RECOVERY_METHOD_UNAVAILABLE,
            "Admin accounts must use the dedicated admin recovery flow",
        )

    if await _user_requires_mfa(db, user):
        return await _start_user_recovery_txn(db, user)

    if not new_password:
        raise BizError(CommonErr.INVALID_INPUT, "new_password is required")
    await _reset_password(db, user, new_password)
    return {"message": "Password reset successful"}


async def _start_user_recovery_txn(db: AsyncSession, user: User) -> dict[str, Any]:
    """为启用了 MFA 的用户创建恢复事务，并返回requires_2fa 详情，以便调用方在重置前完成 2FA。"""
    txn_id = _generate_recovery_txn_id()
    expiry = expires_at(minutes=15)

    contact = user.email or user.phone or ""
    txn = RecoveryTransaction(
        txn_id=txn_id,
        user_id=user.id,
        contact=contact,
        contact_verified=True,
        totp_verified=False,
        consumed=False,
        state="second_factor_pending",
        expires_at=expiry,
    )
    db.add(txn)
    await db.flush()

    temp_token = create_temp_token(user.id, purpose="recovery", txn_id=txn_id)

    return {
        "message": "MFA required. Complete 2FA to finish password reset.",
        "requires_2fa": True,
        "txn_id": txn_id,
        "temp_token": temp_token,
    }


def _generate_recovery_txn_id() -> str:
    return secrets.token_hex(32)


async def recover_admin_begin(
    db: AsyncSession,
    contact: str,
    background_tasks: BackgroundTasksLike | None = None,
) -> dict[str, Any]:
    """第 1 步：启动管理员恢复。服务层负责生成验证码并通过 background_tasks 发送。"""
    user = (
        (
            await db.execute(
                select(User).where((User.email == contact) | (User.phone == contact))
            )
        )
        .scalars()
        .first()
    )

    if user and str(user.account_level) == "admin":
        txn_id = _generate_recovery_txn_id()
        expiry = expires_at(minutes=15)

        txn = RecoveryTransaction(
            txn_id=txn_id,
            user_id=user.id,
            contact=contact,
            contact_verified=False,
            totp_verified=False,
            consumed=False,
            state="contact_pending",
            expires_at=expiry,
        )
        db.add(txn)
        await db.flush()

        channel = channel_for(contact)
        await check_code_rate_limit(
            f"recover:admin:{contact}", max_count=3, window=3600
        )
        code, _ = await channel.create_verification(db, contact, "reset")
        if background_tasks is not None:
            cast(Any, background_tasks).add_task(channel.send_code, contact, code)

        return {
            "message": "If the account is eligible, recovery instructions have been sent.",
            "txn_id": txn_id,
        }

    await check_code_rate_limit(f"recover:admin:{contact}", max_count=3, window=3600)
    await dummy_verify()
    return {
        "message": "If the account is eligible, recovery instructions will be sent to the registered contact."
    }


async def _get_recovery_txn(db: AsyncSession, txn_id: str) -> RecoveryTransaction:
    txn = await get_or_raise(
        db,
        RecoveryTransaction,
        AuthErr.TOKEN_INVALID,
        RecoveryTransaction.txn_id == txn_id,
        detail="Invalid recovery transaction",
    )
    if txn.consumed:
        raise BizError(AuthErr.TOKEN_INVALID, "Recovery transaction already used")
    if txn.expires_at <= now_iso():
        raise BizError(AuthErr.TOKEN_EXPIRED, "Recovery transaction expired")
    return txn


async def recover_admin_verify_contact(
    db: AsyncSession, txn_id: str, code: str
) -> dict[str, Any]:
    """第 2 步：在恢复事务中验证管理员的邮箱/手机验证码。"""
    txn = await _get_recovery_txn(db, txn_id)

    channel = channel_for(txn.contact)
    await channel.consume_code(db, txn.contact, code, "reset")

    txn.contact_verified = True
    await db.flush()

    temp_token = create_temp_token(txn.user_id, purpose="recovery", txn_id=txn_id)

    return {
        "message": "Contact verified. Proceed to 2FA verification.",
        "txn_id": txn_id,
        "temp_token": temp_token,
    }


async def recover_admin_verify_totp(
    db: AsyncSession, txn_id: str, temp_token: str
) -> dict[str, Any]:
    """第 3 步：确认管理员已通过此恢复事务的 2FA 验证。"""
    txn = await _get_recovery_txn(db, txn_id)

    if not txn.contact_verified:
        raise BizError(
            AuthErr.RECOVERY_METHOD_UNAVAILABLE, "Contact verification required first"
        )

    try:
        payload = cast(
            dict[str, Any], cast(Any, security.decode_temp_token)(temp_token)
        )
    except Exception as exc:
        raise BizError(AuthErr.TOKEN_INVALID, "Invalid 2FA temp token") from exc

    user_id: Any = payload.get("user_id", payload.get("sub"))
    if user_id != txn.user_id:
        raise BizError(
            AuthErr.TOKEN_INVALID, "Token user does not match recovery transaction user"
        )

    if payload.get("purpose") != "recovery":
        raise BizError(AuthErr.TOKEN_INVALID, "Token was not issued for recovery")

    if payload.get("txn_id") != txn_id:
        raise BizError(
            AuthErr.TOKEN_INVALID, "Token does not match this recovery transaction"
        )

    # 必须已被 /auth/2fa/verify 消费 —— 在成功的 2FA 之后
    token_hash = hashlib.sha256(temp_token.encode()).hexdigest()
    usage = (
        (
            await db.execute(
                select(TempTokenUsage).where(
                    TempTokenUsage.token_hash == token_hash,
                    TempTokenUsage.user_id == user_id,
                    TempTokenUsage.purpose == "recovery",
                    TempTokenUsage.txn_id == txn_id,
                    TempTokenUsage.consumed.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )
    if not usage:
        raise BizError(
            AuthErr.TOKEN_INVALID, "Temp token not verified – complete 2FA first"
        )

    txn.totp_verified = True
    await db.flush()

    return {
        "message": "2FA verified. You may now set a new password.",
        "txn_id": txn_id,
    }


async def _consume_recovery_txn(db: AsyncSession, txn_id: str) -> User:
    """原子消费恢复事务，返回关联的用户。"""
    now = now_iso()

    if not await consume_once(
        db,
        RecoveryTransaction,
        {"consumed": True, "completed_at": now},
        RecoveryTransaction.txn_id == txn_id,
        RecoveryTransaction.consumed.is_(False),
        RecoveryTransaction.contact_verified.is_(True),
        RecoveryTransaction.totp_verified.is_(True),
        RecoveryTransaction.expires_at > now,
    ):
        raise BizError(
            AuthErr.TOKEN_INVALID, "Recovery transaction invalid or already consumed"
        )

    txn = await get_or_raise(
        db,
        RecoveryTransaction,
        AuthErr.TOKEN_INVALID,
        RecoveryTransaction.txn_id == txn_id,
    )

    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.id == int(txn.user_id),
    )

    return user


async def recover_user_complete(
    db: AsyncSession, txn_id: str, new_password: str
) -> dict[str, Any]:
    """在 2FA 之后完成用户（非管理员）的恢复事务。"""
    user = await _consume_recovery_txn(db, txn_id)

    if str(user.account_level) == "admin":
        raise BizError(
            AuthErr.RECOVERY_METHOD_UNAVAILABLE,
            "Admin accounts must use the dedicated admin recovery flow",
        )

    await _reset_password(db, user, new_password)

    return {"message": "Password reset successful"}


async def recover_admin_complete(
    db: AsyncSession, txn_id: str, new_password: str
) -> dict[str, Any]:
    """第 4 步：使用新密码原子地完成管理员恢复。使用条件 UPDATE 确保只有一个调用方会成功。"""
    user = await _consume_recovery_txn(db, txn_id)

    if str(user.account_level) != "admin":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    await _reset_password(db, user, new_password)

    return {"message": "Password reset successful"}
