"""注册准入状态（P2 申请制注册）：pending/rejected 的判定与登录/发令牌门禁。

与 ``is_locked`` 分离：``is_locked`` 是失败次数/封禁语义，``admission_status`` 只表达
「准不准进」。``LKM_ADMISSION_MODE=apply`` 时自注册用户落 ``pending``；管理员 approve
转 ``active``，reject 转 ``rejected`` 并撤销既有会话（token_version + refresh）。

门禁顺序（防状态泄露）：密码/验证码/凭证校验**通过之后**才调 :func:`assert_admission_allowed`；
锁定判定仍在验密前（用 INVALID_CREDENTIALS 防枚举）。
"""

from __future__ import annotations

from typing import Any

from core.err import AuthErr, BizError

ACTIVE = "active"
PENDING = "pending"
REJECTED = "rejected"

VALID_STATUSES: tuple[str, ...] = (ACTIVE, PENDING, REJECTED)


def admission_status_of(user: Any) -> str:
    """取准入状态；未 flush 的新行/测试桩无该属性时按 active 处理。"""
    return str(getattr(user, "admission_status", ACTIVE) or ACTIVE)


def admission_is_active(user: Any) -> bool:
    return admission_status_of(user) == ACTIVE


def assert_admission_allowed(user: Any) -> None:
    """登录/发令牌门禁：pending 提示待审，rejected 带拒绝原因。"""
    status = admission_status_of(user)
    if status == PENDING:
        raise BizError(AuthErr.ADMISSION_PENDING, "注册申请待管理员审批")
    if status == REJECTED:
        detail = getattr(user, "reject_reason", None) or "注册申请已被拒绝"
        raise BizError(AuthErr.ADMISSION_REJECTED, detail)
