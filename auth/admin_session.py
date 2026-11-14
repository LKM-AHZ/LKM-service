"""后台 cookie 会话基元（AUTH 域单一事实源，S5-A2）。

admin 会话真值收进 auth 域后，签发/校验/清空后台 cookie 所需的全部纯函数与常量
集中在 auth 域此模块，供两处消费且不违反 auth owner-leaf：

- **auth 会话写面**（``auth.admin_router``，登录/刷新/登出/2FA，DB 走 auth 库）签名与
  撤销；admin_session 是该权威侧的直接源码依赖。
- **单体内 me 面**（``app/modules/admin`` 的登录态读取/2FA gate）仅需校验既有 cookie，
  不再写 auth 表；它单向 import auth 合规，方向 admin→auth 不触发 owner-leaf。

后台 access cookie 与前台分离的 audience：后台只认专属 ``aud lkm:admin`` 且
``type=admin``，防被前台或 temp token 冒用。

*db-less、纯函数*：本模块只依赖 ``core``（config/err）+ ``core.db.base``(now_iso)，
不带任何 auth 表/session —— 让签名端(auth)与校验端(admin)都能孤立复用。

后台 cookie 常量（名字/路径/audience/窗口）的单一事实源在 ``core.ports.authz``：
app 侧受 import-linter 约束不得 import auth，只能从 core 取，故常量放 core 契约层，
签约/校验实现在本模块。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import jwt

from auth import jwt_keys
from core.err import BizError, CommonErr
from core.ports.authz import (
    _ADMIN_AUD,
    ACCESS_TOKEN_MINUTES,
    COOKIE_NAME,
    COOKIE_PATH,
    MFA_TRUST_SECONDS,
    REFRESH_NAME,
)


def create_admin_access_token(
    user: Any,
    mfa_verified: bool = False,
    mfa_at: int | None = None,
    active_roles: tuple[str, ...] | None = None,
    session_expires_at: datetime.datetime | None = None,
    refresh_token_hash: str | None = None,
) -> str:
    """签发后台 access token（15min）。payload 带 type=admin + 专属 audience。

    token_version 编入 payload：改密/登出提升版本号后旧 cookie 立即失效（与前台一致）。
    mfa_verified=True 时把 mfa 标记与验证时刻(mfa_at)编入，供危险操作 step-up 校验。
    mfa_at 可显式传入（admin refresh 继承原信任时刻），默认取当前时间。

    *user* 只要暴露 ``id/account_level/token_version``（auth.User 或等形 ORM 行）；
    取值经 str()/int() 收敛，避免强绑定具体 auth 模型类型（auth_router 与单体内复用）。
    """
    now = datetime.datetime.now(datetime.UTC)
    verified_at = mfa_at if mfa_at is not None else int(now.timestamp())
    access_expires_at = int(
        (now + datetime.timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp()
    )
    if session_expires_at is not None:
        access_expires_at = min(access_expires_at, int(session_expires_at.timestamp()))
    payload: dict[str, object] = {
        "sub": str(user.id),
        "account_level": str(user.account_level),
        "type": "admin",
        "aud": _ADMIN_AUD,
        # 单 token 标识：admin 登出按 jti 写黑名单即时失效该 cookie，而不 bump token_version
        # （后者会把该 admin 的所有设备一并踢掉，见 auth.token_revocation 的分工说明）。
        "jti": uuid.uuid4().hex,
        # APISIX jwt-auth 靠该 claim 查消费者（见 jwt_keys.GATEWAY_KEY）：
        "key": jwt_keys.GATEWAY_KEY,
        "token_version": int(user.token_version),
        "mfa": mfa_verified,
        "mfa_at": verified_at if mfa_verified else None,
        "iat": int(now.timestamp()),
        "exp": access_expires_at,
    }
    if active_roles is not None:
        payload["active_roles"] = list(active_roles)
    if refresh_token_hash is not None:
        payload["rt_hash"] = refresh_token_hash
    return jwt_keys.encode(payload)


def decode_admin_access(token: str) -> dict[str, Any]:
    """解签后台 access cookie，校验 audience/type；非法抛 FORBIDDEN。

    纯函数：只依赖 local jwt_keys（RS256 公钥），无 DB 侧写；供单体内 me/危险操作
    的 require danger 复用（仅校验现 cookie 不写 auth 表）。
    """
    try:
        payload = jwt_keys.decode(token, audience=_ADMIN_AUD)
    except (
        jwt.ExpiredSignatureError,
        jwt.InvalidSignatureError,
        jwt.InvalidTokenError,
        jwt.DecodeError,
    ):
        raise BizError(
            CommonErr.FORBIDDEN, "Admin session invalid or expired"
        ) from None
    if payload.get("type") != "admin":
        raise BizError(CommonErr.FORBIDDEN, "Not an admin session token")
    return payload


# -- 面向星号导出再暴露（兼容 monolith 现有调用），非数据 --
__all__ = [
    "ACCESS_TOKEN_MINUTES",
    "COOKIE_NAME",
    "COOKIE_PATH",
    "MFA_TRUST_SECONDS",
    "REFRESH_NAME",
    "_ADMIN_AUD",
    "create_admin_access_token",
    "decode_admin_access",
]
