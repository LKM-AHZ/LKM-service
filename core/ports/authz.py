"""
鉴权端口：JWT 依赖注入与裁决能力的包装。
FastAPI 的 ``Header``/``Depends`` 包装留在这里（保证注入语义与 OpenAPI 文档不变），
真正的解码与裁决由 auth 绑定进来的实现完成。**未绑定即抛 PortNotBound**，绝不匿名放行。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import Depends, Header
from jwt import PyJWTError

from core.config import settings
from core.contracts import CurrentUser
from core.err import AuthErr, BizError, CommonErr
from core.ports.registry import get

_LEVEL_ORDER = {"local": 0, "normal": 1, "admin": 2}

# ---- 后台会话（admin cookie 面）契约常量：app 侧只读使用，签约实现留在 auth ----
COOKIE_NAME = "admin_session"
REFRESH_NAME = "admin_refresh"
# 与 cookie max_age 同源（auth 侧 _set_access_cookie 用 settings.admin_access_cookie_minutes）：
# 写死 15 时运维只改 settings 会让 JWT exp 与 cookie 存活期漂移（cookie 还在但请求全 403）
ACCESS_TOKEN_MINUTES = settings.admin_access_cookie_minutes
# 与前台分离的 audience：后台 access cookie 只认本 audience，防被其它会话冒用。
_ADMIN_AUD = "lkm:admin"
COOKIE_PATH = f"/{settings.api_prefix.strip('/')}"
# 危险操作 step-up 2FA 的信任窗口：验证通过后 1 小时内不再重复要求（前台与后台同值）。
MFA_TRUST_SECONDS = 3600


def seam_enabled() -> bool:
    """鉴权缝（authz HTTP seam）是否启用（拆库后 auth 是唯一真值）。"""
    return get("authz").seam_enabled()


def decode_access_token(token: str) -> Mapping[str, Any]:
    """解码并校验访问令牌，失败抛 ``PyJWTError``/``ValueError``。"""
    return get("authz").decode_access_token(token)


async def resolve_current_user(token: str, db: Any = None) -> CurrentUser:
    """裁决一个令牌对应的当前用户（非 Depends 路径用，如 WebSocket）。

    ``db`` 缺省时由实现自开 auth 会话；调用方若持有可查 users 的会话（融合部署/测试）
    可传入以复用。
    """
    return await get("authz").resolve_current_user(token, db)


async def resolve_via_seam(
    user_id: uuid.UUID,
    expect_token_version: int,
    iat_ts: object,
    *,
    require_admin: bool,
    jti: str | None = None,
) -> CurrentUser:
    """经 auth 内部 authz 裁决一次会话并重建 CurrentUser（后台 seam 权威裁决）。"""
    return await get("authz").resolve_via_seam(
        user_id,
        expect_token_version,
        iat_ts,
        require_admin=require_admin,
        jti=jti,
    )


def create_admin_access_token(
    user: Any, mfa_verified: bool = False, mfa_at: int | None = None
) -> str:
    """签发后台访问令牌（cookie 面）。"""
    return get("authz").create_admin_access_token(
        user, mfa_verified=mfa_verified, mfa_at=mfa_at
    )


def decode_admin_access(token: str) -> Mapping[str, Any]:
    """解码后台访问令牌（不查库，仅载荷）。"""
    return get("authz").decode_admin_access(token)


async def is_jti_blocked(jti: str | None) -> bool:
    """jti 是否已被撤销（Redis 预检）。"""
    return await get("authz").is_jti_blocked(jti)


def _bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 头取出 Bearer 令牌；缺失/格式不对返回 None。"""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def _parse_bearer(
    authorization: str | None = Header(None, alias="Authorization"),
) -> str:
    """提取 Bearer 令牌；缺失/格式错误抛 BizError(FORBIDDEN)。"""
    token = _bearer_token(authorization)
    if token is not None:
        return token
    if not authorization:
        raise BizError(CommonErr.FORBIDDEN, "Missing authorization header")
    raise BizError(CommonErr.FORBIDDEN, "Invalid authorization header format")


async def auth_session() -> AsyncIterator[Any]:
    """FastAPI 依赖：提供 auth realm 会话（提交/回滚/关闭由实现负责）。

    单独暴露成依赖对象，是为了让测试能 ``app.dependency_overrides`` 到自己的隔离库
    （后端路由的鉴权会话不再由调用方 new_session 自持）。生产由 auth 绑定
    ``auth.db.session.get_auth_session``。
    """
    dep = get("authz_session")
    async for session in dep():
        yield session


async def get_current_user(
    token: str = Depends(_parse_bearer),
    db: Any = Depends(auth_session),
) -> CurrentUser:
    """必选 JWT 认证依赖。"""
    return await get("authz").resolve_current_user(token, db)


async def get_optional_user(
    authorization: str | None = Header(None, alias="Authorization"),
    db: Any = Depends(auth_session),
) -> CurrentUser | None:
    """可选 JWT 认证依赖（无令牌/令牌无效均返回 None，不抛错）。"""
    token = _bearer_token(authorization)
    if token is None:
        return None
    try:
        return await get("authz").resolve_current_user(token, db)
    except (BizError, PyJWTError) as exc:
        import logging

        logging.getLogger("lkm.ports.authz").debug("optional auth ignored: %s", exc)
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


async def get_current_user_2fa(
    token: str = Depends(_parse_bearer),
    cur: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """前台危险操作依赖：在有效会话之上，另要求本会话已通过 step-up 2FA 且信任未过期。

    校验失败抛 CommonErr.MFA_REQUIRED，前端据此弹 TOTP 验证（POST /auth/2fa/step-up）后重试。
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
    if time.time() - tried_at > MFA_TRUST_SECONDS:
        raise BizError(CommonErr.MFA_REQUIRED, "MFA trust expired")
    return cur


require_2fa = Depends(get_current_user_2fa)
