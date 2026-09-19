"""设置路由 -- 邮箱 / 手机号绑定端点。

所有端点都需要已登录用户（get_current_user）。

POST /auth/settings/bind-email/request  {email}  -> 通过 EmailProvider 发送验证码
POST /auth/settings/bind-email/verify   {email, code} -> 绑定 + 如果是本地用户则升级
POST /auth/settings/bind-phone/request  {phone}  -> 通过 SmsProvider 发送验证码
POST /auth/settings/bind-phone/verify   {phone, code} -> 绑定 + 如果是本地用户则升级
"""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.common import ApiResp
from app.core.err import BizError, CommonErr, respond
from app.db.repo import get_or_raise
from auth import service_2fa, service_auth
from auth.db.session import get_auth_session
from auth.deps import (
    CurrentUser,
    get_current_user,
    get_email_provider,
    get_sms_provider,
)
from auth.errors import AuthErr
from auth.models import User, UserOAuth
from auth.providers.base import EmailProvider, SmsProvider
from auth.schemas import (
    BindCodeRequestResponse,
    BindCodeVerifyResponse,
    MessageResponse,
    RawEmail,
    SettingsInfo,
    UnbindRequest,
)
from auth.service_verify import (
    check_code_rate_limit,
    consume_email_code,
    consume_phone_code,
    create_email_verification,
    create_phone_verification,
)

router = APIRouter(prefix="/auth/settings", tags=["auth-settings"])

_BIND_PURPOSE = "bind"


class BindEmailRequest(BaseModel):
    email: RawEmail


class BindEmailVerify(BaseModel):
    email: RawEmail
    code: str = Field(..., min_length=6, max_length=6)


class BindPhoneRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=20)


class BindPhoneVerify(BaseModel):
    phone: str = Field(..., min_length=5, max_length=20)
    code: str = Field(..., min_length=6, max_length=6)


@router.post("/bind-email/request", response_model=ApiResp[BindCodeRequestResponse])
@respond
async def bind_email_request(
    body: BindEmailRequest,
    background_tasks: BackgroundTasks,
    _cur: CurrentUser = Depends(get_current_user),
    email_provider: EmailProvider = Depends(get_email_provider),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """请求用于绑定的邮箱验证码。"""
    # 检查邮箱是否已被占用
    existing = (
        (await db.execute(select(User).where(User.email == body.email)))
        .scalars()
        .first()
    )
    if existing:
        raise BizError(
            AuthErr.ALREADY_REGISTERED, "Email already bound to another account"
        )

    rate_limit_key = f"bind_email:{body.email}"
    await check_code_rate_limit(rate_limit_key)

    code, record_id = await create_email_verification(db, body.email, _BIND_PURPOSE)
    background_tasks.add_task(email_provider.send_code, body.email, code)

    return {"message": "Verification code sent to email", "record_id": record_id}


@router.post("/bind-email/verify", response_model=ApiResp[BindCodeVerifyResponse])
@respond
async def bind_email_verify(
    body: BindEmailVerify,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """验证邮箱验证码并将邮箱绑定到当前用户。"""
    await consume_email_code(db, body.email, body.code, _BIND_PURPOSE)

    # 确保邮箱仍未被占用（可能在请求和验证之间被占用）
    existing = (
        (await db.execute(select(User).where(User.email == body.email)))
        .scalars()
        .first()
    )
    if existing:
        raise BizError(
            AuthErr.ALREADY_REGISTERED, "Email already bound to another account"
        )

    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.id == cur.id,
        options=(selectinload(User.oauth_bindings),),
    )

    user.email = body.email
    await db.flush()

    # 如果适用，将本地用户升级为普通用户
    await service_auth.upgrade_to_normal(db, user)

    return {"message": "Email bound successfully"}


@router.post("/bind-phone/request", response_model=ApiResp[BindCodeRequestResponse])
@respond
async def bind_phone_request(
    body: BindPhoneRequest,
    background_tasks: BackgroundTasks,
    _cur: CurrentUser = Depends(get_current_user),
    sms_provider: SmsProvider = Depends(get_sms_provider),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """请求用于绑定的短信验证码。"""
    # 检查手机号是否已被占用
    existing = (
        (await db.execute(select(User).where(User.phone == body.phone)))
        .scalars()
        .first()
    )
    if existing:
        raise BizError(
            AuthErr.ALREADY_REGISTERED, "Phone already bound to another account"
        )

    rate_limit_key = f"bind_phone:{body.phone}"
    await check_code_rate_limit(rate_limit_key)

    code, record_id = await create_phone_verification(db, body.phone, _BIND_PURPOSE)
    background_tasks.add_task(sms_provider.send_code, body.phone, code)

    return {"message": "Verification code sent to phone", "record_id": record_id}


@router.post("/bind-phone/verify", response_model=ApiResp[BindCodeVerifyResponse])
@respond
async def bind_phone_verify(
    body: BindPhoneVerify,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """验证手机验证码并将手机号绑定到当前用户。"""
    await consume_phone_code(db, body.phone, body.code, _BIND_PURPOSE)

    # 确保手机号仍未被占用
    existing = (
        (await db.execute(select(User).where(User.phone == body.phone)))
        .scalars()
        .first()
    )
    if existing:
        raise BizError(
            AuthErr.ALREADY_REGISTERED, "Phone already bound to another account"
        )

    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.id == cur.id,
        options=(selectinload(User.oauth_bindings),),
    )

    user.phone = body.phone
    await db.flush()

    # 如果适用，将本地用户升级为普通用户
    await service_auth.upgrade_to_normal(db, user)

    return {"message": "Phone bound successfully"}


@router.get("", response_model=ApiResp[SettingsInfo])
@respond
async def get_settings(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """返回当前用户的绑定状态（邮箱 / 手机号 / GitHub / 2FA）。"""
    user = await get_or_raise(db, User, AuthErr.USER_NOT_FOUND, User.id == cur.id)
    gh = (
        (
            await db.execute(
                select(UserOAuth).where(
                    UserOAuth.user_id == cur.id, UserOAuth.provider == "github"
                )
            )
        )
        .scalars()
        .first()
    )
    totp = await service_2fa.get_enabled_totp(db, cur.id)
    return {
        "email": user.email or None,
        "phone": user.phone or None,
        "github": gh.provider_email if gh else None,
        "has_2fa": totp is not None,
    }


_ALLOWED_UNBIND = {"email", "phone", "github"}


@router.delete("/{binding_type}", response_model=ApiResp[MessageResponse])
@respond
async def unbind(
    binding_type: str,
    body: UnbindRequest,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """解绑邮箱 / 手机号 / GitHub。已开启 2FA 时需携带 TOTP 码或恢复码二次验证。"""
    if binding_type not in _ALLOWED_UNBIND:
        raise BizError(
            CommonErr.INVALID_INPUT, f"Unsupported binding type: {binding_type}"
        )

    user = await get_or_raise(
        db,
        User,
        AuthErr.USER_NOT_FOUND,
        User.id == cur.id,
        options=(selectinload(User.oauth_bindings),),
    )

    # 2FA 门槛：三种解绑（email/phone/github）已开启 2FA 时都必须二次验证（TOTP 或恢复码）
    totp = await service_2fa.get_enabled_totp(db, cur.id)
    if totp is not None:
        if not body.code and not body.recovery_code:
            raise BizError(AuthErr.TOTP_CODE_INVALID, "2FA 已开启，解绑需要动态验证码")
        await service_2fa.verify_second_factor(
            db, cur.id, code=body.code, recovery_code=body.recovery_code
        )

    # 保留至少一种登录方式：normal 用户要求 email/phone/github 至少留一个
    if _count_login_ways(user) <= 1:
        raise BizError(
            CommonErr.INVALID_INPUT,
            "至少需要保留一种登录方式（邮箱/手机号/GitHub）",
        )

    if binding_type == "email":
        user.email = None
    elif binding_type == "phone":
        user.phone = None
    else:  # github
        result = await db.execute(
            sa_delete(UserOAuth).where(
                UserOAuth.user_id == cur.id, UserOAuth.provider == "github"
            )
        )
        if (getattr(result, "rowcount", 0) or 0) == 0:
            raise BizError(CommonErr.INVALID_INPUT, "GitHub 尚未绑定")
    await db.flush()
    return {"message": f"{binding_type} unbound"}


def _count_login_ways(user: User) -> int:
    count = 0
    if user.email:
        count += 1
    if user.phone:
        count += 1
    if user.oauth_bindings:
        count += 1
    return count
