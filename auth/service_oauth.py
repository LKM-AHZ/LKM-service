"""OAuth 服务 —— 授权 URL、处理回调、绑定账户。

账户关联逻辑（查绑定 → 按邮箱找用户 → 建新用户）与提供商无关，
提供商只负责 authorize_url / exchange_code / fetch_user 三个动作。
"""

import hashlib
import hmac
import re
import secrets
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.exc import IntegrityError

from auth.errors import AuthErr
from auth.models import OAuthState, User
from auth.providers.github import GithubOAuth
from auth.providers.oauth import get_provider
from auth.repository import (
    OAuthDesktopCodeRepository,
    OAuthStateRepository,
    UserOAuthRepository,
    UserRepository,
    is_integrity_error,
)
from auth.service_auth import (
    _check_account_locked,
    build_registration_response,
    create_user_with_profile,
    ensure_unique_username,
    finalize_auth_response,
    log_audit,
    upgrade_to_normal,
)
from core.db.base import expires_at, now_iso
from core.db.repo import consume_once, get_or_raise
from core.db.repository import DbSession
from core.err import BizError

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_CHALLENGE_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_desktop_redirect(raw: str | None) -> str | None:
    """Only loopback HTTP(S) redirects are allowed: tokens must never be sent
    to an attacker-controlled host. Fragments are rejected because the desktop
    endpoint appends its own query parameters."""
    if raw is None or raw.strip() == "":
        return None
    value = raw.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise BizError(
            AuthErr.OAUTH_PROVIDER_ERROR,
            "Desktop redirect must be a loopback http(s) URL",
        )
    return value


def normalize_desktop_challenge(
    raw: str | None, redirect: str | None
) -> str | None:
    """A desktop redirect requires the PKCE-style SHA-256 challenge, so a
    process that races the loopback port still cannot redeem the code."""
    if redirect is None:
        return None
    challenge = (raw or "").strip().lower()
    if not _CHALLENGE_RE.fullmatch(challenge):
        raise BizError(
            AuthErr.OAUTH_PROVIDER_ERROR,
            "Desktop redirect requires a SHA-256 challenge",
        )
    return challenge


def desktop_redirect_url(
    base: str, *, code: str | None = None, error: str | None = None
) -> str:
    """Append `code` or `error` to the stored loopback redirect, keeping any
    query the desktop already put there and never emitting a fragment."""
    parsed = urlsplit(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if code is not None:
        query["code"] = code
    if error is not None:
        query["error"] = error
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
    )


async def peek_oauth_state(
    db: DbSession, state: str, purpose: str
) -> OAuthState | None:
    """Read an unconsumed state row without consuming it, so the callback can
    still redirect a failure to the desktop loopback."""
    return await OAuthStateRepository(db).get_one(
        OAuthState.state == state,
        OAuthState.consumed.is_(False),
        OAuthState.purpose == purpose,
        OAuthState.expires_at > now_iso(),
    )


def _json_safe(value: Any) -> Any:
    """AuthTokenData 里的 ``user_id`` 是 UUID，JSON 列不能直接存，统一转字符串。"""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def create_desktop_code(
    db: DbSession, result: dict[str, Any], challenge: str
) -> str:
    code = secrets.token_urlsafe(32)
    await OAuthDesktopCodeRepository(db).create(
        code=code,
        challenge=challenge,
        result=_json_safe(result),
        expires_at=expires_at(minutes=2),
    )
    return code


async def exchange_desktop_code(
    db: DbSession, code: str, verifier: str
) -> dict[str, Any]:
    """Redeem a desktop code with its PKCE verifier. The code is single-use;
    a wrong verifier does not consume it, so a mistyped input can be retried."""
    repo = OAuthDesktopCodeRepository(db)
    row = await repo.get_one(*repo.consume_conditions(code, now_iso()))
    if row is None:
        raise BizError(AuthErr.TOKEN_INVALID, "Invalid or expired desktop code")
    expected = hashlib.sha256(verifier.encode()).hexdigest()
    if not hmac.compare_digest(row.challenge, expected):
        raise BizError(AuthErr.TOKEN_INVALID, "Desktop verifier mismatch")
    consumed = await consume_once(
        db,
        type(row),
        {"consumed": True},
        *repo.consume_conditions(code, now_iso()),
    )
    if not consumed:
        raise BizError(AuthErr.TOKEN_INVALID, "Desktop code already used")
    return dict(row.result)


async def generate_oauth_state(
    db: DbSession,
    purpose: str,
    user_id: uuid.UUID | None = None,
    desktop_redirect: str | None = None,
    desktop_challenge: str | None = None,
) -> str:
    """生成一个高熵的 OAuth state 令牌，存储并返回它。bind 场景关联发起用户。"""
    state = secrets.token_urlsafe(32)
    expiry = expires_at(minutes=10)
    await OAuthStateRepository(db).create(
        state=state,
        purpose=purpose,
        user_id=user_id,
        desktop_redirect=desktop_redirect,
        desktop_challenge=desktop_challenge,
        expires_at=expiry,
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
    db: DbSession,
    purpose: str = "login",
    user_id: uuid.UUID | None = None,
    desktop_redirect: str | None = None,
    desktop_challenge: str | None = None,
) -> str:
    """兼容入口：GitHub 授权 URL。"""
    return await get_oauth_auth_url(
        db,
        GithubOAuth.name,
        purpose,
        user_id,
        desktop_redirect=desktop_redirect,
        desktop_challenge=desktop_challenge,
    )


async def get_oauth_auth_url(
    db: DbSession,
    provider_name: str,
    purpose: str = "login",
    user_id: uuid.UUID | None = None,
    *,
    desktop_redirect: str | None = None,
    desktop_challenge: str | None = None,
) -> str:
    state = await generate_oauth_state(
        db,
        purpose,
        user_id,
        desktop_redirect=desktop_redirect,
        desktop_challenge=desktop_challenge,
    )
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

    # apply 模式：新建的 OAuth 账号同样是待审申请，不能直接发令牌。
    return await build_registration_response(db, user)


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
