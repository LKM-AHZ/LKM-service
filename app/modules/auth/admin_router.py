"""后台 cookie 会话写面 —— AUTH 域自足版（S5-A2 Step0，additive）。

admin 会话真值收进 auth 域后，本 router 承载后台 4 个**写面**端点（登录/刷新/登出/
2FA step-up），DB 走 独立 auth 库（``app.db.auth_session.get_auth_session``），把 auth 域
已迁表的 ``users/profiles/refresh_tokens/totp`` 作为真值（去单独体 biz 库的 users）。

纯新增、不改单体现成文件（admin domain 的 auth_router.py 仍留着直至 Step1 才摘）。

*owner-leaf 合规*：本模块**只 import auth 域内部 + app.core + app.db.base/err/session**，
绝不 import 业务域（admin/rbac/board...）。签名/校验所用后台 cookie 基元（COOKIE_NAME、
COOKIE_PATH、_ADMIN_AUD、create_admin_access_token 等）取自
``app.modules.auth.admin_session``（auth 域单一事实源），不复制第二份。

行为语义与 URL 均对齐单体现行 ``app/modules/admin/auth_router.py``（该文件 admin 4 写面
的迁移）：prefix ``/admin/auth``（挂上 AUTH 进程 api_prefix 后 URL 形如 ``/api/v1/admin/
auth/login``），前后台分离的 cookie 名 + audience 不变。
"""

from __future__ import annotations

import datetime
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.err import BizError, CommonErr, resp_json
from app.core.secrets import reveal
from app.db.auth_session import get_auth_session
from app.db.base import now_iso
from app.db.repo import consume_once, get_or_raise
from app.modules.auth.admin_session import (
    _ADMIN_AUD,
    COOKIE_NAME,
    COOKIE_PATH,
    MFA_TRUST_SECONDS,
    REFRESH_NAME,
    create_admin_access_token,
)
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import RefreshToken, User
from app.modules.auth.schemas import Password
from app.modules.auth.security import verifypwd
from app.modules.auth.service_2fa import verify_user_totp
from app.modules.auth.service_auth import generate_refresh_token, hash_refresh_token
from app.modules.auth.service_verify import check_code_rate_limit

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


# -- 请求 schema（就地私有，避免依赖业务 admin schemas） -----------------------


class _AdminLoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: Password


class _AdminVerify2FARequest(BaseModel):
    code: str = Field(..., min_length=1)


def _current_mfa_trust(request: Request) -> tuple[bool, int | None]:
    """解析当前 access cookie 的 2FA 信任状态，供 refresh 继承（避免信任被 15min cookie 过期截断）。

    语义与单体现行 admin/auth_router 完全一致；token 缺失/失效/非 admin/过期一律视为未信任。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False, None
    try:
        payload = jwt.decode(
            token,
            reveal(settings.jwt_secret),
            algorithms=[settings.jwt_algorithm],
            audience=_ADMIN_AUD,
        )
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
        return False, None
    if payload.get("type") != "admin" or not payload.get("mfa"):
        return False, None
    mfa_at = payload.get("mfa_at")
    if mfa_at is None:
        return False, None
    trusted_until = datetime.datetime.fromtimestamp(
        float(mfa_at), tz=datetime.UTC
    ) + datetime.timedelta(seconds=MFA_TRUST_SECONDS)
    if trusted_until < datetime.datetime.now(datetime.UTC):
        return False, None
    return True, int(mfa_at)


# -- cookie helper（复用 admin_session 常量，行为对齐单体现行）-----------------


def _set_access_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.admin_access_cookie_minutes * 60,
        path=COOKIE_PATH,
    )


def _set_refresh_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        key=REFRESH_NAME,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 86400,
        path=COOKIE_PATH,
    )


def _clear_cookies(resp: Response) -> None:
    resp.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    resp.delete_cookie(REFRESH_NAME, path=COOKIE_PATH)


def _admin_user_dict(user: User) -> dict[str, Any]:
    """后台返回的管理员自身信息（对齐单体现行 AdminUserOut 字段，无 PII 富字段）。"""
    return {
        "id": int(user.id),
        "username": str(user.username),
        "account_level": str(user.account_level),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# -- 2FA step-up 需先确认当前会话是合法 admin（复用 admin_session 基元，本地裁决于 auth 库）--
async def _require_admin_from_cookie(request: Request, db: AsyncSession) -> User:
    """按 admin access cookie 识别当前登录管理员（auth 库自足复刻 get_current_admin 语义）。

    相比单体现行依赖业务 admin deps 的 seam 裁决，此处**直连 current DBA=false 只在 auth
    进程**读 auth 库：解析 admin_session cookie → 取 user → 强制 account_level=admin +
    锁定 / token_version / updated_at 校验（与前台一致，防绕过安全态）。非法一律 FORBIDDEN。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise BizError(CommonErr.FORBIDDEN, "Not logged into admin panel")

    payload = jwt.decode(
        token,
        reveal(settings.jwt_secret),
        algorithms=[settings.jwt_algorithm],
        audience=_ADMIN_AUD,
    )
    if payload.get("type") != "admin":
        raise BizError(CommonErr.FORBIDDEN, "Not an admin session token")
    sub = payload.get("sub")
    try:
        user_id = int(sub) if sub is not None else 0
    except (TypeError, ValueError):
        raise BizError(CommonErr.FORBIDDEN, "Admin session subject invalid") from None

    user = await get_or_raise(db, User, AuthErr.USER_NOT_FOUND, User.id == user_id)
    if user.is_locked and user.locked_until and user.locked_until > now_iso():
        raise BizError(CommonErr.FORBIDDEN, "Admin account is locked")
    if int(payload.get("token_version", 0)) != int(user.token_version):
        raise BizError(CommonErr.FORBIDDEN, "Admin session invalidated")
    # 改密撤销：JWT iat >= user.updated_at（允许 5 秒容差），与前台一致
    if user.updated_at:
        token_iat = payload.get("iat")
        if token_iat is not None:
            token_time = datetime.datetime.fromtimestamp(
                float(token_iat), tz=datetime.UTC
            )
            if user.updated_at - token_time > datetime.timedelta(seconds=5):
                raise BizError(
                    CommonErr.FORBIDDEN, "Admin session invalidated – password changed"
                )
    if user.account_level != "admin":
        raise BizError(CommonErr.FORBIDDEN, "Insufficient admin privilege")
    return user


@router.post("/login")
async def admin_login(
    body: _AdminLoginReq,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """管理员密码登录（auth 库真值，httpOnly cookie 会话）。

    频控两把锁（方案 §8.4）：用户名级 5/5min + 真实 IP 级 20/5min；IP 源
    ``core.client_ip``（网关后读 ``X-Real-IP``）。用 ``request.client.host`` 会拿到
    apisix 容器地址，使这把锁退化成全站共享单桶。
    """
    await check_code_rate_limit(
        f"admin:login:user:{body.username}", max_count=5, window=300
    )
    await check_code_rate_limit(
        f"admin:login:ip:{client_ip(request)}", max_count=20, window=300
    )

    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalars().first()

    # 统一 403：不区分"用户不存在"密码错，避免枚举账号
    if not user or not await verifypwd(body.password, user.hashed_password):
        return resp_json(CommonErr.FORBIDDEN, detail="用户名或密码错误")

    if user.account_level != "admin":
        return resp_json(CommonErr.FORBIDDEN, detail="无后台访问权限")

    if user.is_locked and user.locked_until and user.locked_until > now_iso():
        return resp_json(CommonErr.FORBIDDEN, detail="账号已锁定")

    # 会话体在 commit 前快照（避免 commit 后 expire 引发的异步重载）
    access_token = create_admin_access_token(
        user
    )  # 读 id/account_level/token_version（已加载）
    payload = _admin_user_dict(user)  # 读 created_at 等（已加载）

    raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            kind="admin",
            mfa_verified=False,
            expires_at=now_iso()
            + datetime.timedelta(days=settings.refresh_token_expire_days),
            revoked_at=None,
        )
    )
    await db.commit()

    resp = resp_json(CommonErr.OK, data=payload)
    _set_access_cookie(resp, access_token)
    _set_refresh_cookie(resp, raw_refresh)
    return resp


@router.post("/refresh")
async def admin_refresh(
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """用 refresh cookie 换新 access + 旋转新 refresh（auth 库原子 consume_once 复用检测）。"""
    await check_code_rate_limit("admin:token:refresh:global", max_count=30, window=60)

    raw_refresh = request.cookies.get(REFRESH_NAME)
    if not raw_refresh:
        return resp_json(CommonErr.FORBIDDEN, detail="缺少刷新令牌")

    tok_hash = hash_refresh_token(raw_refresh)
    now = now_iso()
    if not await consume_once(
        db,
        RefreshToken,
        {"revoked_at": now},
        RefreshToken.token_hash == tok_hash,
        RefreshToken.kind == "admin",
        RefreshToken.revoked_at.is_(None),
    ):
        return resp_json(CommonErr.FORBIDDEN, detail="刷新令牌无效")

    stored = await get_or_raise(
        db,
        RefreshToken,
        AuthErr.TOKEN_INVALID,
        RefreshToken.token_hash == tok_hash,
    )
    if stored.expires_at <= now:
        return resp_json(CommonErr.FORBIDDEN, detail="会话已过期")

    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.id == stored.user_id
    )
    if user.account_level != "admin":
        return resp_json(CommonErr.FORBIDDEN, detail="会话无效")

    # 继承当前 access cookie 的 2FA 信任，避免 15min cookie 轮换打断 1h 信任窗口
    mfa_ok, mfa_at = _current_mfa_trust(request)
    access_token = create_admin_access_token(user, mfa_verified=mfa_ok, mfa_at=mfa_at)
    payload = _admin_user_dict(user)

    new_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(new_refresh),
            kind="admin",
            mfa_verified=mfa_ok,
            expires_at=now_iso()
            + datetime.timedelta(days=settings.refresh_token_expire_days),
            revoked_at=None,
        )
    )
    await db.commit()

    resp = resp_json(CommonErr.OK, data=payload)
    _set_access_cookie(resp, access_token)
    _set_refresh_cookie(resp, new_refresh)
    return resp


@router.post("/logout")
async def admin_logout(
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """登出：auth 库撤销对应 admin refresh 并清空 cookie。"""
    raw_refresh = request.cookies.get(REFRESH_NAME)
    if raw_refresh:
        tok_hash = hash_refresh_token(raw_refresh)
        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == tok_hash,
                RefreshToken.kind == "admin",
            )
        )
        stored = result.scalars().first()
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = now_iso()
            await db.commit()
    resp = resp_json(CommonErr.OK, data={"ok": True})
    _clear_cookies(resp)
    return resp


@router.post("/2fa")
async def admin_verify_2fa(
    body: _AdminVerify2FARequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """危险操作 step-up（auth 库）：验证当前 admin 的 TOTP，通过后签带 2FA 信任的新 access cookie。

    信任窗口 1 小时（MFA_TRUST_SECONDS），未通过不更新信任、仅抛 TOTP_CODE_INVALID
    （经 verify_user_totp 内部 BizError）。
    """
    # 识别当前 admin（auth 库裁决）：无效/非 admin → FORBIDDEN
    user = await _require_admin_from_cookie(request, db)
    await verify_user_totp(db, int(user.id), body.code)

    mfa_at = int(datetime.datetime.now(datetime.UTC).timestamp())
    access_token = create_admin_access_token(user, mfa_verified=True, mfa_at=mfa_at)
    payload = _admin_user_dict(user)

    # 同步更新当前会话 refresh 记录的 mfa 状态（保持一致性，供审计/未来扩展）
    raw_refresh = request.cookies.get(REFRESH_NAME)
    if raw_refresh:
        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(raw_refresh),
                RefreshToken.kind == "admin",
            )
        )
        stored_refresh = result.scalars().first()
        if stored_refresh is not None and stored_refresh.revoked_at is None:
            stored_refresh.mfa_verified = True
    await db.commit()

    resp = resp_json(
        CommonErr.OK, data={**payload, "mfa_verified": True, "mfa_at": mfa_at}
    )
    _set_access_cookie(resp, access_token)
    return resp
