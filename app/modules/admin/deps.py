"""后台 cookie 会话鉴权依赖（S5-A2 Step1：seam-only / 纯缝裁决）。

权限单一事实源在此：能进后台 = 持有有效后台 access cookie 且 **auth 权威**（internal
authz seam）裁决 account_level == "admin" 的存活会话。

拆库后（S5）business 库不再有 auth ``users`` 表，admin 会话的权威 states（锁定 /
token_version 提升 / 改密撤销 / role / account_level）现只由 **auth 域** 持有。因此本模块
不再本地 ``select(User)``：``get_current_admin`` / danger(``get_current_admin_2fa``) 只经
``auth`` internal authz seam(HTTP) 或 **fail-closed 拒**。后台 cookie 的签发/校验基元
（create/decode/aud/常量）唯一事实源在 auth 包，此处经 ``auth.seams`` 单向
import 复用（方向 admin→auth，owner-leaf 合规）。

- seam 未启用（auth_http_url&&token 未配齐）→ 一律 FORBIDDEN 拒，后台绝不保守放行。
- 危险操作依赖（``require_admin_2fa``）在有效 admin seam 会话之上额外校验 cookie 的
  2FA 信任（step-up 1 小时窗口，与前台一致），未过抛 ``MFA_REQUIRED``(code=4)。
"""

import datetime
import uuid
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.db.session import get_session
from auth.deps import (  # 复用其字段契约 + authz seam 开关
    CurrentUser,
    seam_enabled,
)
from auth.seams import (
    _ADMIN_AUD,
    ACCESS_TOKEN_MINUTES,
    COOKIE_NAME,  # 后台 access cookie 常量单一事实源
    COOKIE_PATH,
    MFA_TRUST_SECONDS,  # 2FA step-up 信任窗口常量单一事实源
    REFRESH_NAME,
    create_admin_access_token,  # 签发基元单一事实源（测试/同域复用）
    decode_admin_access,  # 解签/校验 audience+type（纯函数，无 DB 写）
)

# —— 后台 cookie 签发/校验纯基元与常量：单一事实源 auth.admin_session，此处原样 re-export，
#    供既有调用方（auth_router / users / content / moderation / reports …及需造后台 cookie 的
#    拆库 seam 测试）从 admin.deps 向后兼容导入，绝不复制第二份值 ——
__all__ = [
    "ACCESS_TOKEN_MINUTES",
    "COOKIE_NAME",
    "COOKIE_PATH",
    "MFA_TRUST_SECONDS",
    "REFRESH_NAME",
    "_ADMIN_AUD",
    "create_admin_access_token",
    "decode_admin_access",
    "get_current_admin",
    "get_current_admin_2fa",
    "require_admin",
    "require_admin_2fa",
]


def _read_admin_cookie(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME)


async def get_current_admin(
    request: Request,
    token: str | None = Depends(_read_admin_cookie),
    db: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """解析后台 access cookie → seam-only：经 auth internal authz 裁决存活 admin 会话。

    seam 未启用 / 裁决失败 / 网络不可用 → fail-closed 一律 FORBIDDEN（后台绝不保守放行、
    也绝不回落本地读 auth users——business 库本就没有 auth 表）。

    role/account_level 用 auth 权威值（seam verdict），不再本地判锁定/version/改密撤销。
    保留 ``db`` 形参仅为兼容既有直呼方/端点签名契约（不做任何 ``select(User)``）；本函数
    不再依赖 DB 会话。
    """
    if not token:
        raise BizError(CommonErr.FORBIDDEN, "Not logged into admin panel")

    payload = decode_admin_access(token)  # 解签并校验 aud/type；非法抛 FORBIDDEN
    sub = payload.get("sub")
    if not sub:
        raise BizError(CommonErr.FORBIDDEN, "Admin session missing subject")
    # admin JWT 的 sub 是 str(user.id)，user.id 现为 UUID；解析失败保持原 FORBIDDEN 语义（非 500）。
    # 先 str() 再解析：拒绝把裸 int 当 128-bit UUID 接受。
    try:
        user_id = uuid.UUID(str(sub))
    except (AttributeError, TypeError, ValueError):
        raise BizError(CommonErr.FORBIDDEN, "Admin session subject invalid") from None

    # seam-only：未配置 authz seam → fail-closed（business 无本地 auth 真值可判，宁可拒）
    if not seam_enabled():
        raise BizError(CommonErr.FORBIDDEN, "Admin auth service not configured")

    return await _resolve_admin_via_seam(
        user_id,
        int(payload.get("token_version", 0)),
        payload.get("iat"),
    )


async def _resolve_admin_via_seam(
    user_id: uuid.UUID, expect_token_version: int, iat_ts: object
) -> CurrentUser:
    """后台 seam 判定：复用 auth 的 seam 解析（require_admin=True），并把失败统一为 FORBIDDEN。

    seam 拿不到权威裁决（auth 不可用/超时）→ fail-closed 一律 FORBIDDEN（后台绝不保守放行）。
    """
    from auth.seams import resolve_via_seam

    try:
        return await resolve_via_seam(
            user_id, expect_token_version, iat_ts, require_admin=True
        )
    except BizError:
        raise BizError(
            CommonErr.FORBIDDEN, "Admin account state invalid or unavailable"
        ) from None


require_admin = Depends(get_current_admin)


async def get_current_admin_2fa(
    request: Request,
    admin: CurrentUser = Depends(get_current_admin),
) -> CurrentUser:
    """后台危险操作依赖：在 （seam-only 裁决）有效 admin 会话之上，另要求本会话已通过 2FA 且信任未过期（1 小时）。

    校验失败抛 CommonErr.MFA_REQUIRED，前端据此弹出 2FA 验证（POST admin 进程
    /auth/2fa 生成本会话 mfa cookie）后重试。信任解析用 admin_session.decode_admin_access
    纯函数只判现 cookie，不写 auth 表。
    """
    token = _read_admin_cookie(request)
    if not token:
        raise BizError(CommonErr.MFA_REQUIRED, "MFA required")
    payload = decode_admin_access(token)
    if not payload.get("mfa"):
        raise BizError(CommonErr.MFA_REQUIRED, "MFA required")
    mfa_at = payload.get("mfa_at")
    if mfa_at is None:
        raise BizError(CommonErr.MFA_REQUIRED, "MFA required")
    tried_at: Any = datetime.datetime.fromtimestamp(float(mfa_at), tz=datetime.UTC)
    trusted_until = tried_at + datetime.timedelta(seconds=MFA_TRUST_SECONDS)
    if trusted_until < datetime.datetime.now(datetime.UTC):
        raise BizError(CommonErr.MFA_REQUIRED, "MFA trust expired")
    return admin


require_admin_2fa = Depends(get_current_admin_2fa)


