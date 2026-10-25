"""准入审核（P2）：待审列表 + approve/reject 决策。

全部在 auth 权威侧（users 表所在库）：
- 列表给管理员看（含 email，内部缝；不对外公网）。
- approve → ``active``；reject → ``rejected`` + 拒绝原因 + ``last_rejected_at``，
  同时 ``token_version += 1`` 并撤销全部 refresh 会话（已登录的待审账号立即失效）。
- 每次决策写 ``log_audit``（user_id=被审用户，detail 带 actor/decision/reason）。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import func, select

from auth.admission import ACTIVE, PENDING, REJECTED
from auth.errors import AuthErr
from auth.models import User
from auth.repository import RefreshTokenRepository, UserRepository
from auth.service_auth import log_audit
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError, CommonErr

Decision = Literal["approve", "reject"]

_MAX_LIST = 200


def _row_to_dict(user: User) -> dict[str, Any]:
    return {
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "account_level": str(user.account_level),
        "admission_status": str(user.admission_status or ACTIVE),
        "apply_reason": user.apply_reason,
        "applied_at": user.applied_at.isoformat() if user.applied_at else None,
        "reviewed_at": (
            user.admission_reviewed_at.isoformat()
            if user.admission_reviewed_at
            else None
        ),
        "reject_reason": user.reject_reason,
    }


async def count_admission_users(db: DbSession, status: str) -> int:
    """按准入状态计数（后台角标/待办聚合用，不拉行）。"""
    if status not in (ACTIVE, PENDING, REJECTED):
        raise BizError(CommonErr.INVALID_INPUT, f"unknown admission status {status!r}")
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.admission_status == status)
            )
        ).scalar_one()
    )


async def list_admission_users(db: DbSession, status: str) -> list[dict[str, Any]]:
    if status not in (ACTIVE, PENDING, REJECTED):
        raise BizError(CommonErr.INVALID_INPUT, f"unknown admission status {status!r}")
    rows = (
        (
            await db.execute(
                select(User)
                .where(User.admission_status == status)
                .order_by(User.applied_at.desc().nullslast(), User.id.desc())
                .limit(_MAX_LIST)
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_dict(row) for row in rows]


async def decide_admission(
    db: DbSession,
    *,
    user_id: uuid.UUID,
    action: Decision,
    reviewer_id: uuid.UUID,
    reason: str | None = None,
) -> dict[str, Any]:
    """审核一个 pending 申请；非 pending 一律 409（防状态翻转/重复审批）。"""
    user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if user is None:
        raise BizError(AuthErr.USER_NOT_FOUND)
    if str(user.admission_status or ACTIVE) != PENDING:
        raise BizError(CommonErr.CONFLICT, "该申请已审核或不在待审状态")

    now = now_iso()
    user.admission_reviewed_at = now
    user.admission_reviewed_by = reviewer_id
    if action == "approve":
        user.admission_status = ACTIVE
        user.reject_reason = None
        audit_action = "admission_approved"
    elif action == "reject":
        user.admission_status = REJECTED
        user.reject_reason = (reason or "").strip() or "管理员拒绝"
        user.last_rejected_at = now
        audit_action = "admission_rejected"
        # 已登录的待审账号立即失效：版本递增 + 撤销全部 refresh。
        await UserRepository(db).bump_token_version(user.id)
        await RefreshTokenRepository(db).revoke_all_for_user(user.id, now)
    else:
        raise BizError(CommonErr.INVALID_INPUT, f"unknown action {action!r}")

    await UserRepository(db).flush()
    await log_audit(
        db,
        user.id,
        audit_action,
        detail=(
            f"by admin={reviewer_id} decision={action} "
            f"reason={user.reject_reason or ''}"
        ),
    )
    return _row_to_dict(user)
