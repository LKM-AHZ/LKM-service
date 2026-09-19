"""auth 对 app 侧暴露的跨域能力面（公开面之一）。

app 侧（业务域 / flows / worker 装配 / 运维脚本）不得直接 import auth 内部模块；
需要的零散能力统一从这里取。绝大多数条目是**纯 re-export**；少数需要在运行时重新取
内部属性（便于测试 monkeypatch 生效），用薄包装函数承载，见 ``get_channel`` /
``open_session_pair``。
"""

from __future__ import annotations

from typing import Any

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
    "log_audit",
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
    "verifypwd",
]


def get_channel(channel_key: str) -> Any:
    """按 key 取联系通道（发送降级路径用）。

    每次调用重新读 ``auth.channels.CHANNELS``，使测试对该表 monkeypatch 依然生效；
    调用方（``app.core.jobs``）因此不得自行缓存返回值。
    """
    from auth.channels import CHANNELS

    return CHANNELS[channel_key]


async def open_session_pair() -> Any:
    """开跨 realm 双会话（源=auth 只读 / 目标=业务可写），供 user_dim ETL 编排。

    同因惰性：内部重新取 ``auth.user_dim_sync._session_factory``，保持测试 monkeypatch
    该工厂的既有能力。
    """
    from auth import user_dim_sync as _uds

    return await _uds._session_factory()
