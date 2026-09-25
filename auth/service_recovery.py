"""密码恢复服务。"""

import hashlib
import secrets
from typing import Any, cast

from app.core.err import BizError, CommonErr
from app.db.base import expires_at, now_iso
from app.db.repo import consume_once, get_or_raise
from app.db.repository import DbSession
from auth import events, security
from auth.channels import CHANNELS, channel_for
from auth.errors import AuthErr
from auth.limits import RECOVER_ADMIN_BEGIN_MAX, RECOVER_ADMIN_BEGIN_WINDOW
from auth.models import MagicLink, RecoveryTransaction, User
from auth.repository import (
    RecoveryTransactionRepository,
    TempTokenUsageRepository,
    UserRepository,
)
from auth.security import (
    create_temp_token,
    dummy_verify,
    hashpwd,
)
from auth.service_2fa import get_enabled_totp
from auth.service_auth import (
    BackgroundTasksLike,
    log_audit,
    revoke_all_refresh_tokens,
    verify_magic_link,
)
from auth.service_verify import check_code_rate_limit


async def find_user_by_contact(db: DbSession, field: str, value: str) -> User:
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


async def _user_requires_mfa(db: DbSession, user: User) -> bool:
    """如果用户启用了 TOTP 并且必须使用第二因素验证，则返回 True。"""
    if user.account_level == "admin":
        return True
    totp = await get_enabled_totp(db, user.id)
    return totp is not None


async def _reset_password(db: DbSession, user: User, new_password: str) -> None:
    """哈希新密码、设置它、解锁账户、撤销所有令牌，并记录审计日志。"""
    user.hashed_password = await hashpwd(new_password)
    user.is_locked = False
    user.locked_until = None
    user.failed_login_attempts = 0
    user.updated_at = now_iso()  # 使已发放的访问令牌失效（iat < updated_at）
    await UserRepository(db).flush()

    await revoke_all_refresh_tokens(db, user.id)

    await log_audit(db, user.id, "password_reset", detail="recovery")
    # 账户密码重置：解锁 + 清错次 + 抬 updated_at + 全量吊销，属快照相关身份重置 → 失效。
    await events.notify_user_session_revoke(user.id)


async def check_recovery_methods(_db: DbSession, _account: str) -> dict[str, Any]:
    """检查账户可用的恢复方法。"""
    # 始终统一 —— 不泄露账户是否存在、是否为 local 或 admin
    return {"recoverable": False}


async def recover_by_contact(
    db: DbSession, contact: str, code: str, new_password: str | None = None
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
    db: DbSession, token: str, new_password: str | None = None
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


async def _start_user_recovery_txn(db: DbSession, user: User) -> dict[str, Any]:
    """为启用了 MFA 的用户创建恢复事务，并返回requires_2fa 详情，以便调用方在重置前完成 2FA。"""
    txn_id = _generate_recovery_txn_id()
    expiry = expires_at(minutes=15)

    contact = user.email or user.phone or ""
    await RecoveryTransactionRepository(db).create(
        txn_id=txn_id,
        user_id=user.id,
        contact=contact,
        contact_verified=True,
        totp_verified=False,
        consumed=False,
        state="second_factor_pending",
        expires_at=expiry,
    )

    temp_token = create_temp_token(user.id, purpose="recovery", txn_id=txn_id)

    return {
        "message": "MFA required. Complete 2FA to finish password reset.",
        "requires_2fa": True,
        "txn_id": txn_id,
        "temp_token": temp_token,
    }


def _generate_recovery_txn_id() -> str:
    return secrets.token_hex(32)


#: admin 恢复发起的一律文案：两个分支必须逐字相同，否则响应体差异就是 admin 账号枚举 oracle。
_ADMIN_RECOVER_BEGIN_MSG = (
    "If the account is eligible, recovery instructions have been sent."
)


async def recover_admin_begin(
    db: DbSession,
    contact: str,
    background_tasks: BackgroundTasksLike | None = None,
) -> dict[str, Any]:
    """第 1 步：启动管理员恢复。服务层负责生成验证码并通过 background_tasks 发送。"""
    user = await UserRepository(db).find_by_email_or_phone(contact)

    # 恒定时序：无论邮箱/手机是否注册为 admin，都在分支前执行一次等成本的
    # argon2 虚拟哈希——避免「存在=不发散(快)、不存在=跑 dummy_verify(慢)」的
    # 耗时差被攻击者当作账号枚举 oracle。
    await dummy_verify()

    if user and str(user.account_level) == "admin":
        txn_id = _generate_recovery_txn_id()
        expiry = expires_at(minutes=15)

        await RecoveryTransactionRepository(db).create(
            txn_id=txn_id,
            user_id=user.id,
            contact=contact,
            contact_verified=False,
            totp_verified=False,
            consumed=False,
            state="contact_pending",
            expires_at=expiry,
        )

        channel = channel_for(contact)
        await check_code_rate_limit(
            f"recover:admin:{contact}",
            max_count=RECOVER_ADMIN_BEGIN_MAX,
            window=RECOVER_ADMIN_BEGIN_WINDOW,
        )
        code, _ = await channel.create_verification(db, contact, "reset")
        if background_tasks is not None:
            cast(Any, background_tasks).add_task(channel.send_code, contact, code)

        return {"message": _ADMIN_RECOVER_BEGIN_MSG, "txn_id": txn_id}

    await check_code_rate_limit(
        f"recover:admin:{contact}",
        max_count=RECOVER_ADMIN_BEGIN_MAX,
        window=RECOVER_ADMIN_BEGIN_WINDOW,
    )
    # 与命中分支**同文案、同字段**：文案或字段差异本身就是「该联系方式是否属于 admin」的
    # oracle（上方 dummy_verify 维持的恒定时序会被响应体差异抵消）；且缺 txn_id 会让
    # AdminRecoverBeginResponse 响应校验失败，把 200 变成 500。这里返回一个不落库的诱饵
    # txn_id，下一步必然以「事务不存在」失败，语义仍是 fail-closed。
    return {"message": _ADMIN_RECOVER_BEGIN_MSG, "txn_id": _generate_recovery_txn_id()}


async def _get_recovery_txn(db: DbSession, txn_id: str) -> RecoveryTransaction:
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
    db: DbSession, txn_id: str, code: str
) -> dict[str, Any]:
    """第 2 步：在恢复事务中验证管理员的邮箱/手机验证码。"""
    txn = await _get_recovery_txn(db, txn_id)

    channel = channel_for(txn.contact)
    await channel.consume_code(db, txn.contact, code, "reset")

    await RecoveryTransactionRepository(db).update(txn, contact_verified=True)

    temp_token = create_temp_token(txn.user_id, purpose="recovery", txn_id=txn_id)

    return {
        "message": "Contact verified. Proceed to 2FA verification.",
        "txn_id": txn_id,
        "temp_token": temp_token,
    }


async def recover_admin_verify_totp(
    db: DbSession, txn_id: str, temp_token: str
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
    # JWT 落 JSON，uuid 以字符串回读；与 UUID 列比较须归一字符串形式
    if str(user_id) != str(txn.user_id):
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
    usage = await TempTokenUsageRepository(db).find_recovery_usage(
        token_hash=token_hash, user_id=user_id, txn_id=txn_id
    )
    if not usage:
        raise BizError(
            AuthErr.TOKEN_INVALID, "Temp token not verified – complete 2FA first"
        )

    await RecoveryTransactionRepository(db).update(txn, totp_verified=True)

    return {
        "message": "2FA verified. You may now set a new password.",
        "txn_id": txn_id,
    }


async def _consume_recovery_txn(db: DbSession, txn_id: str) -> User:
    """原子消费恢复事务，返回关联的用户。"""
    now = now_iso()

    if not await consume_once(
        db,
        RecoveryTransaction,
        {"consumed": True, "completed_at": now},
        *RecoveryTransactionRepository(db).consume_conditions(txn_id, now),
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
        User.id == txn.user_id,
    )

    return user


async def recover_user_complete(
    db: DbSession, txn_id: str, new_password: str
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
    db: DbSession, txn_id: str, new_password: str
) -> dict[str, Any]:
    """第 4 步：使用新密码原子地完成管理员恢复。使用条件 UPDATE 确保只有一个调用方会成功。"""
    user = await _consume_recovery_txn(db, txn_id)

    if str(user.account_level) != "admin":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    await _reset_password(db, user, new_password)

    return {"message": "Password reset successful"}
