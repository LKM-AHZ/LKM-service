"""后台 cookie 会话基元（AUTH 域单一事实源，S5-A2）。

admin 会话真值收进 auth 域后，签发/校验/清空后台 cookie 所需的全部纯函数与常量
集中在 auth 域此模块，供两处消费且不违反 auth owner-leaf：

- **auth 会话写面**（``auth.admin_router``，登录/刷新/登出/2FA，DB 走 auth 库）签名与
  撤销；admin_session 是该权威侧的直接源码依赖。
- **单体内 me 面**（``app/modules/admin`` 的登录态读取/2FA gate）仅需校验既有 cookie，
  不再写 auth 表；它单向 import auth 合规，方向 admin→auth 不触发 owner-leaf。

后台 access cookie 与前台分离的 audience：后台只认专属 ``aud lkm:admin`` 且
``type=admin``，防被前台或 temp token 冒用。

*db-less、纯函数*：本模块只依赖 ``app.core``（config/err）+ ``app.db.base``(now_iso)，
不带任何 auth 表/session —— 让签名端(auth)与校验端(admin)都能孤立复用。
"""

from __future__ import annotations

import datetime
from typing import Any

import jwt

from app.core.config import settings
from app.core.err import BizError, CommonErr
from app.core.secrets import reveal

COOKIE_NAME = "admin_session"
REFRESH_NAME = "admin_refresh"
ACCESS_TOKEN_MINUTES = 15
# 与前台/后台分离的 audience：后台 access cookie 只认本 audience，防被其它会话冒用。
_ADMIN_AUD = "lkm:admin"
# cookie Path 需覆盖 admin 后台全部路径（含 /api/v1/boards、/projects 等危险操作端点），
# 故扩展到整个 API 前缀而非 /admin 子路径；type=admin + 专属 audience 仍保证前台不认。
COOKIE_PATH = f"/{settings.api_prefix.strip('/')}"
# 危险操作 step-up 2FA 的信任窗口：验证通过后 1 小时内不再重复要求。
MFA_TRUST_SECONDS = 3600


def create_admin_access_token(
    user: Any, mfa_verified: bool = False, mfa_at: int | None = None
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
    # payload 元素类型混杂（str/int/bool），用 object 收窄容器泛型，避免 Unknown
    payload: dict[str, object] = {
        "sub": str(user.id),
        "account_level": str(user.account_level),
        "type": "admin",
        "aud": _ADMIN_AUD,
        "token_version": int(user.token_version),
        "mfa": mfa_verified,
        "mfa_at": verified_at if mfa_verified else None,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + datetime.timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp()
        ),
    }
    return jwt.encode(
        payload, reveal(settings.jwt_secret), algorithm=settings.jwt_algorithm
    )


def decode_admin_access(token: str) -> dict[str, Any]:
    """解签后台 access cookie，校验 audience/type；非法抛 FORBIDDEN。

    纯函数：只依赖 settings.jwt_secret/algorithm，无 DB 侧写；供单体内 me/危险操作
    的 require danger 复用（仅校验现 cookie 不写 auth 表）。
    """
    try:
        payload = jwt.decode(
            token,
            reveal(settings.jwt_secret),
            algorithms=[settings.jwt_algorithm],
            audience=_ADMIN_AUD,
        )
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
