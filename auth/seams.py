"""auth 对 app 侧暴露的跨域能力面（公开面之一）。

app 侧（业务域 / flows / worker 装配 / 运维脚本）不得直接 import auth 内部模块；
需要的零散能力统一从这里取。绝大多数条目是**纯 re-export**；少数需要在运行时重新取
内部属性（便于测试 monkeypatch 生效），用薄包装函数承载，见 ``get_channel`` /
``open_session_pair``。
"""

from __future__ import annotations

from typing import Any

from app.core.err import BizError, CommonErr
from auth.admin_session import (
    _ADMIN_AUD,
    ACCESS_TOKEN_MINUTES,
    COOKIE_NAME,
    COOKIE_PATH,
    MFA_TRUST_SECONDS,
    REFRESH_NAME,
    create_admin_access_token,
    decode_admin_access,
)
from auth.audit_export import export_audit_logs
from auth.db.session import get_auth_session, new_auth_session
from auth.deps import _resolve_current_user as resolve_current_user
from auth.deps import _resolve_via_seam as resolve_via_seam
from auth.deps import get_email_provider, get_sms_provider, seam_enabled
from auth.security import _totp_code as totp_code
from auth.security import _totp_now as totp_now
from auth.security import hashpwd, verifypwd
from auth.service_2fa import setup_2fa_begin, setup_2fa_complete
from auth.service_auth import log_audit
from auth.service_authz import (
    grant_exam_unlock_from_business,
    grant_incubation_from_business,
)
from auth.service_passkey import cleanup_expired_challenges
from auth.token_revocation import block_payload_jti, is_jti_blocked
from auth.user_dim_sync import (
    reconcile_user_dim_incremental,
    reconcile_user_dim_periodic,
    sync_dim_for_ids,
)

__all__ = [
    "ACCESS_TOKEN_MINUTES",
    "COOKIE_NAME",
    "COOKIE_PATH",
    "MFA_TRUST_SECONDS",
    "REFRESH_NAME",
    "_ADMIN_AUD",
    "block_payload_jti",
    "cleanup_expired_challenges",
    "create_admin_access_token",
    "decode_admin_access",
    "export_audit_logs",
    "get_auth_session",
    "get_channel",
    "get_email_provider",
    "get_sms_provider",
    "grant_exam_unlock_from_business",
    "grant_incubation_from_business",
    "hashpwd",
    "is_jti_blocked",
    "log_audit",
    "mint_bot_sso_ticket",
    "new_auth_session",
    "open_session_pair",
    "reconcile_user_dim_incremental",
    "reconcile_user_dim_periodic",
    "resolve_current_user",
    "resolve_via_seam",
    "seam_enabled",
    "setup_2fa_begin",
    "setup_2fa_complete",
    "sync_dim_for_ids",
    "totp_code",
    "totp_now",
    "verify_password_via_seam",
    "verifypwd",
]


def get_channel(channel_key: str) -> Any:
    """按 key 取联系通道（发送降级路径用）。

    每次调用重新读 ``auth.channels.CHANNELS``，使测试对该表 monkeypatch 依然生效；
    调用方（``app.core.jobs``）因此不得自行缓存返回值。
    """
    from auth.channels import CHANNELS

    try:
        return CHANNELS[channel_key]
    except KeyError:
        # 未知 key 是调用方传错值：抛领域错误（可归因、可映射状态码），而不是让裸 KeyError
        # 冒到上层变成不可解释的 500 —— 与本模块其它缝的错误翻译风格保持一致
        raise BizError(CommonErr.INVALID_INPUT, f"unknown channel: {channel_key}") from None


async def open_session_pair() -> Any:
    """开跨 realm 双会话（源=auth 只读 / 目标=业务可写），供 user_dim ETL 编排。

    同因惰性：内部重新取 ``auth.user_dim_sync._session_factory``，保持测试 monkeypatch
    该工厂的既有能力。
    """
    from auth import user_dim_sync as _uds

    return await _uds._session_factory()


async def mint_bot_sso_ticket(
    user_id: Any, account_level: str = "admin"
) -> dict[str, Any]:
    """代表一个已裁决的管理员铸一次性 bot 面板 SSO 票据，返回 ``{"ticket","expires_in"}``。

    票据签发原语在 auth 域（私钥唯一持有方），业务进程只能经内部 HTTP 缝取（拆库后 business
    既无签发私钥也无 auth 真值）。**fail-closed**：缝未配置/不可达/畸形 → 抛
    ``BizError(UNAVAILABLE)``，绝不返回空票让调用方以为「已免登」。

    惰性取内部实现，保持测试对 ``auth.user_http`` 的 monkeypatch 依然生效。
    """
    from auth.user_http import UserHttpUnavailable
    from auth.user_http import mint_bot_sso_ticket as _mint

    try:
        return await _mint(user_id=user_id, account_level=account_level)
    except UserHttpUnavailable as exc:
        raise BizError(CommonErr.UNAVAILABLE, f"Bot SSO ticket unavailable: {exc}") from None
    except Exception as exc:
        # 本函数是上面那条 fail-closed 承诺的唯一落点，但 UserHttpUnavailable 盖不住全部
        # 「不可达/畸形」：auth_http_url 非法时 httpx 抛的是 InvalidURL（ValueError 子类，
        # **不是** httpx.HTTPError），惰性建 client 也可能抛别的。故这里兜底翻译，
        # 原始异常留在 __cause__ 里供排查，绝不让裸异常穿过这条缝。
        raise BizError(CommonErr.UNAVAILABLE, f"Bot SSO ticket unavailable: {exc}") from exc


async def verify_password_via_seam(
    username: str, password: str
) -> dict[str, Any] | None:
    """校验一组 Basic 凭证，返回 ``{"user_id", "username"}``；**权威否答返回 None**。

    拆库后业务库无 users 表，git HTTP-Basic 的验密只能经 auth 内部端点完成（blog git_http
    消费）。None 表示「用户不存在／无密码／口令不匹配」这类权威否答，调用方按未认证收场；
    **缝不可用/畸形则抛 ``BizError(UNAVAILABLE)``**（fail-closed）——凭证校验拿不到真值时
    必须拒绝，绝不能回落业务库直查（那里没有该表）或放行。

    惰性取内部实现，保持测试对 ``auth.user_http`` 的 monkeypatch 依然生效。
    """
    from auth.user_http import UserHttpUnavailable
    from auth.user_http import verify_password_via_seam as _verify

    try:
        payload = await _verify(username=username, password=password)
    except UserHttpUnavailable as exc:
        raise BizError(
            CommonErr.UNAVAILABLE, f"Credential verification unavailable: {exc}"
        ) from None
    except Exception as exc:
        # 同 mint_bot_sso_ticket：InvalidURL 等非 HTTPError 异常也要收口在这条 fail-closed
        # 承诺内，原始异常留在 __cause__ 供排查。
        raise BizError(
            CommonErr.UNAVAILABLE, f"Credential verification unavailable: {exc}"
        ) from exc
    if not payload.get("ok"):
        return None
    return {"user_id": payload["user_id"], "username": payload["username"]}
