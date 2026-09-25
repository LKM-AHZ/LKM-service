"""FastAPI 路由的 JWT 依赖注入。"""

import datetime as _dt
import logging
import os
import time as _time
import uuid
from typing import Any

from fastapi import Depends, Header
from jwt import PyJWTError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from auth.db.session import get_auth_session
from auth.errors import AuthErr
from auth.models import User
from auth.providers.base import EmailProvider, SmsProvider
from auth.providers.console import ConsoleEmailProvider, ConsoleSmsProvider
from auth.security import decode_access_token
from auth.service_authz import (
    CAUSE_LOCKED,
    CAUSE_NOT_ADMIN,
    CAUSE_NOT_FOUND,
    CAUSE_PASSWORD_CHANGED,
    CAUSE_SESSION_REVOKED,
)
from auth.token_revocation import is_jti_blocked

_LEVEL_ORDER = {"local": 0, "normal": 1, "admin": 2}

logger = logging.getLogger(__name__)


class CurrentUser(BaseModel):
    """从已验证的 JWT 访问令牌中提取的用户信息。"""

    id: uuid.UUID
    account_level: str
    role: str
    email: str | None = None
    phone: str | None = None


def _bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 头取出 Bearer 令牌；缺失/格式不对返回 None（与 _parse_bearer 共用同一套解析规则）。"""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def _parse_bearer(
    authorization: str | None = Header(None, alias="Authorization"),
) -> str:
    """从 Authorization 请求头中提取 Bearer 令牌。如果请求头缺失或格式错误，则抛出 BizError(FORBIDDEN)。"""
    token = _bearer_token(authorization)
    if token is not None:
        return token
    if not authorization:
        raise BizError(CommonErr.FORBIDDEN, "Missing authorization header")
    raise BizError(CommonErr.FORBIDDEN, "Invalid authorization header format")


async def _resolve_current_user(token: str, db: AsyncSession) -> CurrentUser:
    """解码令牌并查找用户。任何失败都抛出 BizError。"""
    try:
        payload = decode_access_token(token)
    except (PyJWTError, ValueError) as exc:
        raise BizError(AuthErr.TOKEN_INVALID) from exc

    # jti 撤销预检（§4.2/§5.6）：登出/单设备撤销后**立即**拒；放在 DB 查询之前正是「快速
    # 预检」的意义。命中必拒；Redis 不可用时 is_jti_blocked 返回 False 跳过，由下面的 DB
    # 判据（token_version / 改密撤销）兜底。无 jti 的旧 token 自动跳过（灰度零破坏）。
    # 错误码与 token_version 拒绝保持同码同文案，不因撤销来源不同而改变客户端语义。
    if await is_jti_blocked(payload.get("jti")):
        raise BizError(
            AuthErr.TOKEN_EXPIRED, "Session invalidated – please login again"
        )

    raw_user_id = payload.get("user_id")
    if not raw_user_id:
        raise BizError(AuthErr.TOKEN_INVALID, "Token missing user_id")
    # JWT 载荷只能带字符串（json 无 uuid 类型），读侧统一还原为 UUID
    try:
        user_id = uuid.UUID(str(raw_user_id))
    except ValueError as exc:
        raise BizError(AuthErr.TOKEN_INVALID, "Token user_id malformed") from exc

    # —— M3.B S3 seam：鉴权缝开启时把“锁定/token_version/改密撤销/权威角色档”判给 auth ——
    if seam_enabled():
        return await _resolve_via_seam(
            user_id,
            int(payload.get("token_version", 0)),
            payload.get("iat"),
            require_admin=False,
        )

    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.profile))
    )
    user = result.scalars().first()
    if user is None:
        raise BizError(AuthErr.USER_NOT_FOUND)

    # 检查账号是否被锁定
    if user.is_locked and user.locked_until and user.locked_until > now_iso():
        raise BizError(AuthErr.ACCOUNT_LOCKED)

    # token_version 检查：如果用户的 token_version 已经被提升（例如注销或密码重置后），令牌无效。
    token_ver = payload.get("token_version")
    if token_ver is not None and token_ver != user.token_version:
        raise BizError(
            AuthErr.TOKEN_EXPIRED, "Session invalidated – please login again"
        )

    # 密码更改会撤销现有访问令牌
    # JWT iat 必须 >= user.updated_at（允许 5 秒时钟偏差容差）
    # user.updated_at 为 timezone-aware datetime，可直接与 iat 时间相减
    if user.updated_at:
        token_iat = payload.get("iat")
        if token_iat is not None:
            updated: _dt.datetime = user.updated_at
            token_time = _dt.datetime.fromtimestamp(float(token_iat), tz=_dt.UTC)
            if updated - token_time > _dt.timedelta(seconds=5):
                raise BizError(
                    AuthErr.TOKEN_EXPIRED, "Password changed – please login again"
                )

    profile = user.profile
    role: str = profile.role if profile else "member"
    return CurrentUser(
        id=user.id,
        account_level=str(user.account_level),
        role=role,
        email=user.email,
        phone=user.phone,
    )


def seam_enabled() -> bool:
    """鉴权缝（authz HTTP seam）是否启用：monolith deps 据此把裁决交 auth（拆库后唯一真值）。"""
    from auth import user_http as auth_user_http

    return auth_user_http.enabled()


def _fail_current_user(cause: object | None) -> BizError:
    """把 seam 返回的 cause 映射成与本地路径一致的单点失败 BizError（fail-closed 拒）。"""
    if cause == CAUSE_LOCKED:
        return BizError(AuthErr.ACCOUNT_LOCKED)
    if cause == CAUSE_SESSION_REVOKED:
        return BizError(
            AuthErr.TOKEN_EXPIRED, "Session invalidated – please login again"
        )
    if cause == CAUSE_PASSWORD_CHANGED:
        return BizError(
            AuthErr.TOKEN_EXPIRED, "Password changed – please login again"
        )
    if cause == CAUSE_NOT_FOUND:
        return BizError(AuthErr.USER_NOT_FOUND)
    if cause == CAUSE_NOT_ADMIN:
        return BizError(CommonErr.FORBIDDEN, "Insufficient permission")
    # cause 未知或缺省：一律按不可用拒（fail-closed，鉴权绝不保守放行）
    return BizError(AuthErr.TOKEN_INVALID, "Account state cannot be proven")


async def _resolve_via_seam(
    user_id: uuid.UUID,
    expect_token_version: int,
    iat_ts: object,
    *,
    require_admin: bool,
) -> CurrentUser:
    """经 auth internal authz 裁决一次会话并重建 CurrentUser。

    任何网络/5xx/畸形由 seam 抛 ``UserHttpUnavailable``（fail-closed）：此缝拿不到裁决
    必然判 403/401，绝不“连不上就放行”。返回的 role/account_level 用 auth 权威值。
    """
    from auth import user_http as auth_user_http

    # 规范化“非数值/畸形”的 iat 载荷为一可比较 float；缺失 → None（不检查改密撤销）。
    iat_secs: float | None = None
    if isinstance(iat_ts, (int, float)):
        iat_secs = float(iat_ts)
    elif isinstance(iat_ts, str):
        try:
            iat_secs = float(iat_ts)
        except ValueError:
            iat_secs = None

    try:
        verdict = await auth_user_http.authorize_via_seam(
            user_id=user_id,
            expect_token_version=expect_token_version,
            iat_ts=iat_secs,
            require_admin=require_admin,
        )
    except auth_user_http.UserHttpUnavailable as exc:
        raise BizError(
            AuthErr.TOKEN_INVALID, "Account state service unavailable"
        ) from exc

    if not verdict.get("ok"):
        raise _fail_current_user(verdict.get("cause"))
    return CurrentUser(
        id=user_id,
        account_level=str(verdict.get("account_level") or ""),
        role=str(verdict.get("role") or "member"),
        email=None,
        phone=None,
    )


async def get_current_user(
    token: str = Depends(_parse_bearer),
    db: AsyncSession = Depends(get_auth_session),
) -> CurrentUser:
    """必选 JWT 认证依赖。抛出ERROR"""
    return await _resolve_current_user(token, db)


async def get_optional_user(
    authorization: str | None = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_auth_session),
) -> CurrentUser | None:
    """可选 JWT 认证依赖。不抛出ERROR"""
    # 复用 _bearer_token：否则 scheme 大小写/空白规则会在必选与可选两条路径上各写一遍、日后漂移
    token = _bearer_token(authorization)
    if token is None:
        return None
    try:
        return await _resolve_current_user(token, db)
    except (BizError, PyJWTError) as exc:
        # 可选依赖确实不该抛错，但吞掉要留痕：否则「account state 服务不可用」这类
        # 基础设施故障在线上表现为「偶尔匿名」，没有任何可查的线索
        logger.debug("optional auth ignored: %s", exc)
        return None


def RequireLevel(min_level: str) -> Any:
    """
    级别（从低到高排列）：``local``, ``normal``, ``admin``。
    用法::
        @router.get("/admin-only")
        async def admin_endpoint(cur: CurrentUser = Depends(RequireLevel("admin"))):
            ...
    """

    async def checker(cur: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        required = _LEVEL_ORDER.get(min_level)
        current = _LEVEL_ORDER.get(cur.account_level, 0)
        if required is None or current < required:
            raise BizError(AuthErr.ACCOUNT_LEVEL_INSUFFICIENT)
        return cur

    return Depends(checker)


# 前台危险操作 step-up 2FA 的信任窗口：验证通过后 1 小时内不再重复要求（与后台 admin/deps 同值）
MFA_TRUST_SECONDS = 3600


async def get_current_user_2fa(
    token: str = Depends(_parse_bearer),
    cur: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """前台危险操作依赖：在有效会话之上，另要求本会话已通过 step-up 2FA 且信任未过期（1 小时）。

    校验失败抛 CommonErr.MFA_REQUIRED，前端据此弹 TOTP 验证（POST /auth/2fa/step-up）后重试。
    未启用 TOTP 的用户同样无法满足 mfa 标记，一并引导先启用 2FA（与后台 require_admin_2fa 行为一致）。
    """
    try:
        payload = decode_access_token(token)
    except (PyJWTError, ValueError) as exc:
        raise BizError(CommonErr.MFA_REQUIRED) from exc
    if not payload.get("mfa"):
        raise BizError(CommonErr.MFA_REQUIRED, "MFA required")
    mfa_at = payload.get("mfa_at")
    if mfa_at is None:
        raise BizError(CommonErr.MFA_REQUIRED, "MFA required")
    tried_at = float(mfa_at)
    if _time.time() - tried_at > MFA_TRUST_SECONDS:
        raise BizError(CommonErr.MFA_REQUIRED, "MFA trust expired")
    return cur


require_2fa = Depends(get_current_user_2fa)


def get_sms_provider() -> SmsProvider:
    """返回已配置的短信服务提供商。

    仓库里目前**只有**测试用的 ConsoleSmsProvider（把脱敏验证码写日志），没有任何真实网关
    实现，也没有读取 ``LKM_SMS_PROVIDER`` 的装配代码（该名字此前只出现在下面的报错文本里）。
    故非测试环境一律 fail-closed：绝不退回 console（那等于把验证码写进生产日志）。
    """
    if os.environ.get("LKM_ENV") == "test" or os.environ.get("PYTEST_RUNNING"):
        return ConsoleSmsProvider()
    raise RuntimeError(
        "No SMS provider available: only the test-only ConsoleSmsProvider is implemented and "
        "no real gateway is wired (nothing reads LKM_SMS_PROVIDER). "
        "Implement/register one under auth/providers/ before enabling SMS flows."
    )


def get_email_provider() -> EmailProvider:
    """返回已配置的邮件服务提供商。

    与 :func:`get_sms_provider` 同：只有测试用 ConsoleEmailProvider，非测试环境 fail-closed，
    报错文本不再指向一个无人读取的环境变量。
    """
    if os.environ.get("LKM_ENV") == "test" or os.environ.get("PYTEST_RUNNING"):
        return ConsoleEmailProvider()
    raise RuntimeError(
        "No Email provider available: only the test-only ConsoleEmailProvider is implemented "
        "and no real mail gateway is wired (nothing reads LKM_EMAIL_PROVIDER). "
        "Implement/register one under auth/providers/ before enabling email flows."
    )
