"""OAuth 服务 —— 授权 URL、处理回调、绑定账户。

账户关联逻辑（查绑定 → 按邮箱找用户 → 建新用户）与提供商无关，
提供商只负责 authorize_url / exchange_code / fetch_user 三个动作。
"""

import secrets
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.err import BizError
from app.db.base import expires_at, now_iso
from app.db.repo import consume_once, get_or_raise
from app.db.repository import DbSession
from auth.errors import AuthErr
from auth.models import OAuthState, User
from auth.providers.github import GithubOAuth
from auth.providers.oauth import get_provider
from auth.repository import (
    OAuthStateRepository,
    UserOAuthRepository,
    UserRepository,
    is_integrity_error,
)
from auth.service_auth import (
    _check_account_locked,
    create_user_with_profile,
    ensure_unique_username,
    finalize_auth_response,
    log_audit,
    upgrade_to_normal,
)


async def generate_oauth_state(
    db: DbSession, purpose: str, user_id: uuid.UUID | None = None
) -> str:
    """生成一个高熵的 OAuth state 令牌，存储并返回它。bind 场景关联发起用户。"""
    state = secrets.token_urlsafe(32)
    expiry = expires_at(minutes=10)
    await OAuthStateRepository(db).create(
        state=state, purpose=purpose, user_id=user_id, expires_at=expiry
    )
    return state


async def consume_oauth_state(db: DbSession, state: str, purpose: str) -> OAuthState:
    """消耗一次 OAuth state，返回该记录（含 user_id，供绑定归属）。无效则抛错。"""
    consumed = await consume_once(
        db,
        OAuthState,
        {"consumed": True},
        *OAuthStateRepository(db).consume_conditions(state, purpose, now_iso()),
    )
    if not consumed:
        raise BizError(AuthErr.OAUTH_PROVIDER_ERROR, "Invalid or expired OAuth state")
    row = await get_or_raise(
        db,
        OAuthState,
        AuthErr.OAUTH_PROVIDER_ERROR,
        OAuthState.state == state,
        detail="Invalid or expired OAuth state",
    )
    return row


async def get_github_auth_url(
    db: DbSession, purpose: str = "login", user_id: uuid.UUID | None = None
) -> str:
    """兼容入口：GitHub 授权 URL。"""
    return await get_oauth_auth_url(db, GithubOAuth.name, purpose, user_id)


async def get_oauth_auth_url(
    db: DbSession,
    provider_name: str,
    purpose: str = "login",
    user_id: uuid.UUID | None = None,
) -> str:
    state = await generate_oauth_state(db, purpose, user_id)
    return get_provider(provider_name).authorize_url(state)


async def handle_github_callback(
    db: DbSession, code: str, state: str
) -> dict[str, Any]:
    """兼容入口：GitHub 登录回调。"""
    return await handle_oauth_callback(db, GithubOAuth.name, code, state)


async def handle_oauth_callback(
    db: DbSession, provider_name: str, code: str, state: str
) -> dict[str, Any]:
    provider = get_provider(provider_name)
    await consume_oauth_state(db, state, "login")  # login 场景无需用户归属
    access_token = await provider.exchange_code(code)
    info = await provider.fetch_user(access_token)

    # 1. 现有 OAuth 绑定
    oauth = await UserOAuthRepository(db).find_by_provider_user(
        provider.name, info.provider_user_id
    )
    if oauth:
        user = await UserRepository(db).get_with_profile_or_raise(
            oauth.user_id, AuthErr.USER_NOT_FOUND
        )
        # 既有绑定同样是登录：审计要像 login_password 那样每次成功都记，否则最常见的老用户
        # 复登录在 oauth_login 审计里完全缺席（原先只有「新建用户」分支才有 log_audit）。
        await log_audit(db, user.id, "oauth_login", provider.name)
        return await _oauth_login_response(db, user)

    # 2. 邮箱已被注册 → 拒绝自动绑定登录（需显式绑定后登录，防账号接管）
    if info.provider_email:
        existing = await UserRepository(db).get_by_email(info.provider_email)
        if existing:
            raise BizError(
                AuthErr.OAUTH_EMAIL_ALREADY_REGISTERED,
                "该邮箱已注册，请用密码登录或先在设置中绑定 GitHub",
            )

    # 3. 创建新用户
    username = await ensure_unique_username(db, info.username)
    user = await create_user_with_profile(
        db,
        username=username,
        email=info.provider_email,
        hashed_password="",
        account_level="normal",
    )

    await UserOAuthRepository(db).create(
        user_id=user.id,
        provider=provider.name,
        provider_user_id=info.provider_user_id,
        provider_email=info.provider_email,
    )

    await log_audit(db, user.id, "oauth_login", provider.name)

    return await _oauth_login_response(db, user)


async def bind_github(db: DbSession, code: str, state: str) -> dict[str, Any]:
    """兼容入口：绑定 GitHub 到发起绑定的用户（user_id 由 OAuth state 记录携带）。"""
    return await bind_oauth(db, GithubOAuth.name, code, state)


async def bind_oauth(
    db: DbSession, provider_name: str, code: str, state: str
) -> dict[str, Any]:
    """绑定 OAuth 账户到发起绑定的用户（user_id 由 OAuth state 记录携带，回调无需 JWT）。"""
    provider = get_provider(provider_name)
    st = await consume_oauth_state(db, state, "bind")
    user_id = st.user_id
    if not user_id:
        raise BizError(AuthErr.OAUTH_PROVIDER_ERROR, "Bind session lost its owner")
    access_token = await provider.exchange_code(code)
    info = await provider.fetch_user(access_token)

    existing_oauth = await UserOAuthRepository(db).find_by_provider_user(
        provider.name, info.provider_user_id
    )
    if existing_oauth:
        raise BizError(
            AuthErr.OAUTH_EMAIL_TAKEN,
            f"This {provider.name.capitalize()} account is already bound to another user",
        )

    user = await get_or_raise(db, User, AuthErr.USER_NOT_FOUND, User.id == user_id)

    try:
        await UserOAuthRepository(db).create(
            user_id=user.id,
            provider=provider.name,
            provider_user_id=info.provider_user_id,
            provider_email=info.provider_email,
        )
    except IntegrityError as exc:
        # 上面的存在性检查与这里的 insert 不是原子：两个并发回调可能都通过检查，
        # 由 uq_oauth_provider_user 兜底；翻译成与「已被他人绑定」同一语义的领域错误，
        # 而不是把 IntegrityError 冒成 500。
        if is_integrity_error(exc):
            raise BizError(
                AuthErr.OAUTH_EMAIL_TAKEN,
                f"This {provider.name.capitalize()} account is already bound",
            ) from exc
        raise

    await upgrade_to_normal(db, user)

    return {"message": f"{provider.name.capitalize()} account bound"}


async def _oauth_login_response(db: DbSession, user: User) -> dict[str, Any]:
    """检查锁定与 TOTP 要求后返回认证响应。"""
    # OAuth 回调同样是一条登录入口：is_locked 是封禁标志，其余入口（login_password/
    # login_code）都在发令牌前过 _check_account_locked，此处漏掉会让被封账号经 GitHub 登录。
    await _check_account_locked(user)
    return await finalize_auth_response(db, user)
