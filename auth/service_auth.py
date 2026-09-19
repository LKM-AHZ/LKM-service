"""认证服务 —— 注册、密码登录、升级、刷新令牌。"""

import datetime
import hashlib
import logging
import secrets
import uuid
from typing import Any, Protocol, runtime_checkable

from app.core import jobs
from app.core.config import settings
from app.core.err import BizError, CommonErr
from app.core.throttle import check_password_login_rate_limit
from app.db.base import expires_at, now_iso
from app.db.repo import consume_once, get_or_raise, isolated_update
from app.db.repository import DbSession
from auth import events
from auth.channels import CHANNELS, channel_for
from auth.errors import AuthErr
from auth.models import (
    MagicLink,
    PendingRegistration,
    Profile,
    RefreshToken,
    User,
)
from auth.providers.base import EmailProvider
from auth.repository import (
    AuditLogRepository,
    MagicLinkRepository,
    PendingRegistrationRepository,
    ProfileRepository,
    RefreshTokenRepository,
    TOTPRepository,
    UserRepository,
    is_integrity_error,
    is_operational_error,
)
from auth.schemas import (
    UserLoginPassword,
    UserRegLocal,
)
from auth.security import (
    create_access_token,
    create_temp_token,
    dummy_verify,
    hashpwd,
    verifypwd,
)
from auth.service_verify import (
    check_code_rate_limit,
    consume_email_code,
    consume_phone_code,
)

logger = logging.getLogger(__name__)

_FAIL_LOCK_THRESHOLD = 5
_FAIL_LOCK_MINUTES = 15


@runtime_checkable
class BackgroundTasksLike(Protocol):
    def add_task(self, func: Any, /, *args: Any, **kwargs: Any) -> None: ...


def _normalize_username(username: str) -> str:
    """规范化用户名：仅去除首尾空白，大小写原样保留（大小写绝对敏感）。"""
    return username.strip()


def _normalize_email(email: str) -> str:
    """规范化邮箱：仅去除首尾空白，大小写原样保留（大小写绝对敏感）。"""
    return email.strip()


def generate_refresh_token() -> str:
    """返回一个加密安全的随机十六进制字符串（64 个字符）。"""
    return secrets.token_hex(32)


def hash_refresh_token(raw: str) -> str:
    """对原始刷新令牌进行 SHA-256 哈希。"""
    return hashlib.sha256(raw.encode()).hexdigest()


async def store_refresh_token(
    db: DbSession,
    user_id: uuid.UUID,
    raw: str,
    mfa_verified: bool = False,
    mfa_at: datetime.datetime | None = None,
) -> datetime.datetime:
    """持久化哈希后的刷新令牌并返回其过期时间（timezone-aware datetime）。"""
    days = settings.refresh_token_expire_days
    expires_str = expires_at(days=days)
    await RefreshTokenRepository(db).create(
        user_id=user_id,
        token_hash=hash_refresh_token(raw),
        mfa_verified=mfa_verified,
        mfa_at=mfa_at,
        expires_at=expires_str,
    )
    return expires_str


async def issue_session_tokens(
    db: DbSession,
    user: User,
    *,
    trust_device: bool = False,
    mfa_verified: bool = False,
    mfa_at: datetime.datetime | None = None,
) -> tuple[str, str]:
    """发放访问令牌 + 刷新令牌，返回 (access_token, raw_refresh)。

    mfa_verified=True 时把 step-up 2FA 标记（含信任时刻 mfa_at，默认 now）编入 access token，
    并同步写入刷新记录，供 1 小时信任窗口与刷新轮换继承使用。
    """
    # async 下不能对未加载的 relationship 做 lazy load，统一确保 profile 已初始化
    if "profile" not in user.__dict__:
        await db.refresh(user, attribute_names=["profile"])
    profile = user.profile
    role = profile.role if profile else "member"
    verified_at = mfa_at if mfa_at is not None else datetime.datetime.now(datetime.UTC)
    access_token = create_access_token(
        user_id=user.id,
        account_level=user.account_level,
        role=role,
        trust_device=trust_device,
        token_version=user.token_version,
        mfa_verified=mfa_verified,
        mfa_at=int(verified_at.timestamp()) if mfa_verified else None,
    )
    raw_refresh = generate_refresh_token()
    await store_refresh_token(
        db,
        user.id,
        raw_refresh,
        mfa_verified=mfa_verified,
        mfa_at=verified_at if mfa_verified else None,
    )
    return access_token, raw_refresh


async def _create_auth_response(
    db: DbSession, user: User, requires_2fa: bool = False
) -> dict[str, Any]:
    """构建作为登录 / 注册响应返回的字典。"""
    if requires_2fa:
        temp_token = create_temp_token(user.id)
        return {
            "access_token": None,
            "refresh_token": None,
            "user_id": user.id,
            "account_level": user.account_level,
            "requires_2fa": True,
            "temp_token": temp_token,
        }

    access_token, raw_refresh = await issue_session_tokens(db, user)
    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "user_id": user.id,
        "account_level": user.account_level,
        "requires_2fa": False,
        "temp_token": None,
    }


async def _check_account_locked(user: User) -> None:
    """检查用户是否被锁定 —— 但返回 INVALID_CREDENTIALS 以防止通过锁检测进行账号枚举。"""
    if not user.is_locked:
        return
    if user.locked_until:
        if user.locked_until > now_iso():
            # 执行虚拟哈希以保持时序一致
            await dummy_verify()
            raise BizError(AuthErr.INVALID_CREDENTIALS)
        # 锁定已过期 —— 自动解锁
        user.is_locked = False
        user.locked_until = None
        user.failed_login_attempts = 0


async def _record_failed_attempt(db: DbSession, user: User) -> bool:
    """通过子事务（保存点）递增登录失败计数器，达到阈值时原子锁定。

    自增与锁定判定合并为单条 UPDATE：同一语句内所有列引用都取旧行值，
    由数据库对该行加锁，避免此前「自增后 refresh 再比较」在并发下读到
    自己事务快照、漏锁或延迟触达阈值的竞态。返回是否本次触达锁定阈值。
    """
    await isolated_update(
        db,
        UserRepository(db).failed_login_stmt(
            user.id,
            threshold=_FAIL_LOCK_THRESHOLD,
            lock_minutes=_FAIL_LOCK_MINUTES,
        ),
    )
    await db.refresh(user)

    return user.failed_login_attempts >= _FAIL_LOCK_THRESHOLD


async def ensure_unique_username(db: DbSession, base: str) -> str:
    """在 base 上追加数字后缀直到用户名唯一。"""
    username = base
    suffix = 1
    while await UserRepository(db).username_exists(username):
        username = f"{base}{suffix}"
        suffix += 1
    return username


async def create_user_with_profile(db: DbSession, **fields: Any) -> User:
    """创建用户 + 默认 Profile；唯一性冲突统一转 ALREADY_REGISTERED。"""
    user = User(**fields)
    db.add(user)
    try:
        await UserRepository(db).flush()
    except Exception as exc:
        handle_duplicate_user_error(exc)
    db.add(Profile(user_id=user.id, role="member"))
    await ProfileRepository(db).flush()
    return user


async def register_local(db: DbSession, info: UserRegLocal) -> dict[str, Any]:
    """创建一个 ``local`` 账户，若已存在且密码正确则自动登录。"""
    username = _normalize_username(info.username)
    existing = await UserRepository(db).get_by_username_with_profile(username)
    if existing:
        await _login_or_error(db, existing, info.password)
        return await _create_auth_response(db, existing)

    user = await create_user_with_profile(
        db,
        username=username,
        hashed_password=await hashpwd(info.password),
        account_level="local",
    )

    return await _create_auth_response(db, user)


def handle_duplicate_user_error(exc: Exception) -> None:
    """如果是唯一性违规，将 IntegrityError 重新抛出为 ALREADY_REGISTERED。"""
    if is_integrity_error(exc):
        raise BizError(AuthErr.ALREADY_REGISTERED, "Account already exists") from exc
    raise


async def _login_or_error(
    db: DbSession,
    user: User,
    password_to_check: str,
    *,
    email: str | None = None,
    phone: str | None = None,
) -> User:
    """已有账号时核对密码；正确则补全联系方式并升级为 ``normal``，供各注册入口复用。

    密码缺失或不匹配时报 ``ALREADY_REGISTERED``，避免泄露已有账号信息。
    """
    if not user.hashed_password or not await verifypwd(
        password_to_check, user.hashed_password
    ):
        raise BizError(
            AuthErr.ALREADY_REGISTERED, "Account exists but password is incorrect"
        )
    if email and not user.email:
        user.email = email
    if phone and not user.phone:
        user.phone = phone
    await upgrade_to_normal(db, user)
    await UserRepository(db).flush()
    return user


async def register_by_verify(db: DbSession, field: str, value: str) -> dict[str, Any]:
    """通过邮箱或手机验证创建一个*无密码*的普通用户，若已存在则自动登录。"""
    channel = CHANNELS.get(field)
    if channel is None:
        raise BizError(CommonErr.INVALID_INPUT, "field must be 'email' or 'phone'")

    normalized_value = channel.normalize(value)
    existing = await channel.find_user(db, normalized_value)
    if existing:
        await upgrade_to_normal(db, existing)
        await UserRepository(db).flush()
        await log_audit(db, existing.id, "register_code", f"auto-login via {field}")
        return await _create_auth_response(db, existing)

    username = await ensure_unique_username(db, channel.username_from(value))
    user = await create_user_with_profile(
        db,
        username=username,
        hashed_password="",
        account_level="normal",
        **{field: normalized_value},
    )

    await log_audit(db, user.id, "register_code", f"registered via {field}")
    return await _create_auth_response(db, user)


async def store_pending_normal_registration(
    db: DbSession,
    username: str,
    password: str,
    email: str | None,
    phone: str | None,
) -> str:
    txn_id = secrets.token_hex(32)
    expiry = expires_at(minutes=15)

    await PendingRegistrationRepository(db).create(
        txn_id=txn_id,
        username=_normalize_username(username),
        hashed_password=await hashpwd(password),
        email=_normalize_email(email) if email else None,
        phone=phone,
        consumed=False,
        expires_at=expiry,
    )
    return txn_id


async def consume_pending_normal_registration(
    db: DbSession,
    txn_id: str,
    email_code: str | None = None,
    phone_code: str | None = None,
) -> dict[str, Any]:
    pending = await get_or_raise(
        db,
        PendingRegistration,
        AuthErr.TOKEN_INVALID,
        PendingRegistration.txn_id == txn_id,
        detail="Invalid registration transaction",
    )
    if pending.consumed:
        raise BizError(AuthErr.TOKEN_INVALID, "Registration already completed")
    if pending.expires_at <= now_iso():
        raise BizError(AuthErr.TOKEN_EXPIRED, "Registration expired")

    # 验证所有提交的联系方式 —— 每个提供的联系方式都必须经过验证。
    sp = await db.begin_nested()
    try:
        if pending.email:
            assert email_code is not None
            await consume_email_code(db, str(pending.email), email_code, "register")
        if pending.phone:
            assert phone_code is not None
            await consume_phone_code(db, str(pending.phone), phone_code, "register")
        await sp.commit()
    except Exception as exc:
        if is_integrity_error(exc) or is_operational_error(exc):
            await sp.rollback()
        raise

    await PendingRegistrationRepository(db).update(pending, consumed=True)

    # 检查重复 —— 如果已存在且密码正确则自动登录
    existing = await UserRepository(db).find_for_registration(
        username=str(pending.username),
        email=str(pending.email) if pending.email else None,
        phone=str(pending.phone) if pending.phone else None,
    )
    if existing:
        hashed: str = existing.hashed_password
        pending_hashed: str = pending.hashed_password
        if not hashed or not await verifypwd(pending_hashed, hashed):
            raise BizError(
                AuthErr.ALREADY_REGISTERED, "Account exists but password is incorrect"
            )
        # 将联系方式绑定到已有账户
        if pending.email and not existing.email:
            existing.email = pending.email
        if pending.phone and not existing.phone:
            existing.phone = pending.phone
        await upgrade_to_normal(db, existing)
        await UserRepository(db).flush()
        await log_audit(
            db, existing.id, "register_normal", "auto-login via registration"
        )
        return await _create_auth_response(db, existing)

    user = await create_user_with_profile(
        db,
        username=str(pending.username),
        email=str(pending.email) if pending.email else None,
        phone=str(pending.phone) if pending.phone else None,
        hashed_password=str(pending.hashed_password),
        account_level="normal",
    )

    await log_audit(db, user.id, "register_normal", "password registration complete")
    return await _create_auth_response(db, user)


async def _check_admin_totp_required(
    db: DbSession, user: User
) -> dict[str, Any] | None:
    """如果用户是管理员但尚未设置 TOTP，返回 setup 响应；否则返回 None。"""
    if str(user.account_level) != "admin":
        return None
    totp = await TOTPRepository(db).get_by_user(user.id)
    if totp and totp.enabled:
        return None
    setup_token = create_temp_token(user.id, purpose="setup")
    return {
        "access_token": None,
        "refresh_token": None,
        "user_id": user.id,
        "account_level": str(user.account_level),
        "requires_2fa": True,
        "setup_required": True,
        "temp_token": setup_token,
    }


async def finalize_auth_response(db: DbSession, user: User) -> dict[str, Any]:
    """检查管理员 TOTP 和 2FA 要求，返回认证响应。"""
    admin_setup = await _check_admin_totp_required(db, user)
    if admin_setup is not None:
        return admin_setup

    # 登录不再每次强制 2FA（对齐 GitHub 缓动）：仅"首次强制设置"由上面
    # _check_admin_totp_required 处理；已启用 TOTP 的普通登录直接发会话令牌，
    # 危险操作时再由 admin 后台 step-up 校验（见 require_admin_2fa）。
    return await _create_auth_response(db, user, requires_2fa=False)


async def login_password(
    db: DbSession, info: UserLoginPassword, ip_address: str = ""
) -> dict[str, Any]:
    """通过用户名、邮箱或手机号 + 密码进行认证。"""
    if ip_address:
        await check_password_login_rate_limit(ip_address)

    account = _normalize_username(info.account)
    email_normalized = _normalize_email(info.account)

    user = await UserRepository(db).find_for_login(
        username=account, email=email_normalized, phone=info.account.strip()
    )

    if not user:
        # 防御用户枚举：执行一个相同成本的虚拟哈希，
        await dummy_verify()
        raise BizError(AuthErr.INVALID_CREDENTIALS)

    # M3.A残项(成功登录解锁)：记录**入库持久态**的 is_locked（在 auto-unlock 之前），供下方
    # 成功登录把它 True→False 翻转时发出失效事件——解除 post-user.banned 的 user:snap banned 陈旧。
    # 不可在 auto-unlock(_check_account_locked 会把过期锁的 ORM 值改 False)之后才采样，会漏翻转。
    was_locked_at_login = bool(user.is_locked)

    await _check_account_locked(user)

    try:
        ok = await verifypwd(info.password, str(user.hashed_password))
    except Exception:
        logger.exception(
            "verifypwd raised exception for user_id=%s (possible corrupted hash)",
            user.id,
        )
        ok = False
    if not ok:
        locked = await _record_failed_attempt(db, user)
        if locked:
            await log_audit(db, user.id, "account_locked", "5 failed login attempts")
            # 锁定经 isolated_update 的 savepoint 已提交且本请求将回滚 → 失效事件须自建会话独立
            # 提交，避免与已落库的 is_locked=True 错位（漏失效会让 user:snap.banned 陈旧）。
            await events.notify_user_banned_committed(user.id)
        raise BizError(AuthErr.INVALID_CREDENTIALS)

    # 成功 —— 通过子事务（savepoint）原子性地重置计数器，
    # 防止调用方回滚时把失败计数器也一并回滚。
    await isolated_update(db, UserRepository(db).reset_login_failures_stmt(user.id))
    await db.refresh(user)

    # M3.A残项(成功登录解锁)：若本次成功登录真把 is_locked 从 True 翻到 False（先前自动锁后、
    # 过期锁在此刻被清除），发 user.updated 失效，令下次读 user:snap.banned=False，杜绝陈旧。
    # 只在翻转发生时发（was_locked_at_login=True），普通成功登录 is_locked 恒 False → 零噪声不入队；
    # 本请求为成功路径(outer 会话将 commit)，同事务入队随其持久（与 upgrade 等 A7 载点同款）。若
    # 此处无从翻转(本就 False)则不发——避免无谓回填。镜像 A7「无东西需失效就不发」约束。
    if was_locked_at_login:
        await events.notify_user_updated(db, user.id)

    await log_audit(db, user.id, "login_password", "success")

    return await finalize_auth_response(db, user)


async def login_code(db: DbSession, contact: str, code: str) -> dict[str, Any]:
    """使用有时效性的验证码进行认证。"""
    channel = channel_for(contact)
    await channel.consume_code(db, contact, code, "login")
    user = await channel.find_user(db, channel.normalize(contact))
    if user is None:
        raise BizError(AuthErr.USER_NOT_FOUND)

    if user.account_level == "local":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    if user.is_locked:
        await _check_account_locked(user)

    # 没有 TOTP 的管理员 —— 与密码登录相同的设置流程
    return await finalize_auth_response(db, user)


async def request_magic_link(
    db: DbSession,
    email: str,
    email_provider: EmailProvider,
    purpose: str = "login",
    frontend_url: str = "",
    background_tasks: BackgroundTasksLike | None = None,
) -> None:
    """仅在用户存在时为*邮箱*生成一个魔法链接。

    速率限制为每（邮箱, 用途）对每小时 5 次请求。
    原始令牌为 64 个十六进制字符；仅存储其 SHA-256 哈希值。

    对于不存在的用户，响应和时序无法区分
    —— 不会创建或发送链接，但仍会消耗速率限制配额。
    """
    rate_limit_key = f"magiclink:{email}"
    await check_code_rate_limit(rate_limit_key, max_count=5, window=3600)

    user = await UserRepository(db).get_by_email(email)
    if not user or user.account_level == "local":
        # 无操作：不创建也不发送，但速率限制在上方已被消耗
        return

    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    expiry = expires_at(minutes=15)

    await MagicLinkRepository(db).create(
        email=email,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expiry,
    )

    base_url = frontend_url or settings.api_prefix
    link = f"{base_url}/auth/login/magic-link/verify?token={raw_token}"

    await jobs.send_magic_link(email, link)


async def verify_magic_link(
    db: DbSession,
    token: str,
    purpose: str = "login",
) -> dict[str, Any]:
    """
    验证魔法链接令牌并返回认证响应。
    可能抛出的异常：
        BizError(TOKEN_INVALID)  – 令牌未找到、用途不匹配或已被使用
        BizError(TOKEN_EXPIRED)  – 令牌已过期
        BizError(USER_NOT_FOUND) – 不存在与该链接邮箱关联的用户
        BizError(ACCOUNT_LEVEL_INSUFFICIENT) – 用户为 ``local`` 级别
        BizError(TOTP_SETUP_REQUIRED) – 管理员用户未启用 TOTP
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    now = now_iso()

    # 原子消费：仅在尚未使用、未过期且用途匹配时才标记为已使用。
    # 这可防止并发重放攻击。
    if not await consume_once(
        db,
        MagicLink,
        {"used": True},
        *MagicLinkRepository(db).consume_conditions(token_hash, purpose, now),
    ):
        # 令牌可能已过期或不存在 —— 检查具体是哪一种情况
        link_record = await MagicLinkRepository(db).get_by_hash(token_hash)
        if not link_record:
            raise BizError(AuthErr.TOKEN_INVALID)
        if link_record.purpose != purpose:
            raise BizError(AuthErr.TOKEN_INVALID)
        if link_record.used:
            raise BizError(AuthErr.TOKEN_INVALID)
        # 必然是已过期
        raise BizError(AuthErr.TOKEN_EXPIRED)

    # 原子更新后重新获取
    link_record = await get_or_raise(
        db,
        MagicLink,
        AuthErr.TOKEN_INVALID,
        MagicLink.token_hash == token_hash,
    )

    user = await UserRepository(db).get_by_email_or_raise(
        link_record.email, AuthErr.USER_NOT_FOUND
    )

    if user.account_level == "local":
        raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)

    # 没有 TOTP 的管理员必须设置它
    if user.account_level == "admin":
        totp = await TOTPRepository(db).get_by_user(user.id)
        if not totp or not totp.enabled:
            raise BizError(AuthErr.TOTP_SETUP_REQUIRED)

    # 与 login_password 相同：登录不再每次强制 2FA，直接发会话令牌。
    return await _create_auth_response(db, user, requires_2fa=False)


async def upgrade_to_normal(db: DbSession, user: User) -> None:
    """将 ``local`` 用户升级为 ``normal``。对于已是 normal 或 admin 的用户无操作。"""
    if user.account_level == "local":
        user.account_level = "normal"
        await UserRepository(db).flush()
        await log_audit(db, user.id, "level_change", "local -> normal")
        # 快照 account_level 依赖 User.account_level：升级属身份升迁 → 失效 user:snap。
        await events.notify_user_updated(db, user.id)


async def refresh_access_token(db: DbSession, raw_refresh: str) -> dict[str, Any]:
    tok_hash = hash_refresh_token(raw_refresh)
    now = now_iso()

    # 原子撤销：仅在令牌存在、尚未被撤销且属于 web 端点时才撤销。
    # kind == "web" 过滤：堵住 admin-kind token 被 web 端点轮换的跨会话互用。
    if not await consume_once(
        db,
        RefreshToken,
        {"revoked_at": now},
        *RefreshTokenRepository(db).consume_conditions(tok_hash),
    ):
        # 令牌已被使用、不存在或已被撤销
        raise BizError(AuthErr.TOKEN_INVALID)

    # 现在获取记录以得到 user_id 和 mfa_verified
    stored = await get_or_raise(
        db,
        RefreshToken,
        AuthErr.TOKEN_INVALID,
        RefreshToken.token_hash == tok_hash,
    )

    # 过期检查
    if stored.expires_at <= now:
        raise BizError(AuthErr.TOKEN_EXPIRED)

    # 发放新令牌
    user = await UserRepository(db).get_with_profile_or_raise(
        stored.user_id, AuthErr.USER_NOT_FOUND
    )

    # 登录不再强制 admin MFA（对齐 GitHub 缓动）：刷新会话保持原有保证级别即可，
    # 危险操作的安全由 admin 后台 step-up（require_admin_2fa）在请求时校验。
    # 前台同样继承本会话的 step-up 2FA 信任原点（stored.mfa_at），
    # 避免 15min access token 轮换把危险操作的 1 小时信任窗口重置。
    access_token, raw_new = await issue_session_tokens(
        db, user, mfa_verified=stored.mfa_verified, mfa_at=stored.mfa_at
    )
    return {"access_token": access_token, "refresh_token": raw_new}


async def revoke_all_refresh_tokens(db: DbSession, user_id: uuid.UUID) -> None:
    """撤销指定用户所有未撤销的刷新令牌，并使其所有访问令牌失效。"""
    await RefreshTokenRepository(db).revoke_all_for_user(user_id, now_iso())
    # 递增 token_version 以使所有现有访问令牌失效
    await UserRepository(db).bump_token_version(user_id)
    await UserRepository(db).flush()


async def log_audit(
    db: DbSession,
    user_id: uuid.UUID | None,
    action: str,
    detail: str | None = None,
    ip_address: str | None = None,
) -> None:
    """创建一条审计日志记录。"""
    await AuditLogRepository(db).create(
        user_id=user_id,
        action=action,
        detail=detail,
        ip_address=ip_address,
    )
