"""账号状态门禁：锁定判定的**唯一语义**。

历史上“锁定”在登录、token 依赖、refresh、authz 裁决、admin 会话五处各自写了一遍::

    if user.is_locked and user.locked_until and user.locked_until > now_iso():

该写法把**永久锁定**（``is_locked=True`` 且 ``locked_until=NULL``）当成未锁定放行。
引入准入/封禁语义后，这正是“永久封禁仍能登录”的缺口。本模块收口为单一谓词：

- :func:`lock_is_active`：``is_locked`` 且（无到期时间 = 永久，或尚未到期）。
- :func:`clear_expired_lock`：过期锁的自动解除（仅登录门禁调用，落库由调用方事务负责）。

**新增登录/准入入口（passkey、magic link、未来的 admission）必须复用这里**，不得再写
内联判据；发令牌的统一兜底在 ``service_auth.finalize_auth_response``。
"""

from __future__ import annotations

from auth.models import User
from core.db.base import now_iso


def lock_is_active(user: User) -> bool:
    """账号是否处于生效中的锁定（``locked_until`` 为空视为永久锁定）。"""
    if not user.is_locked:
        return False
    if user.locked_until is None:
        return True
    return user.locked_until > now_iso()


def clear_expired_lock(user: User) -> None:
    """解除**已过期**的临时锁；永久锁（``locked_until`` 为空）不清除。

    只改 ORM 字段，不 flush/commit：调用方（登录门禁）所在事务会把结果落库。
    """
    if not user.is_locked or user.locked_until is None:
        return
    if user.locked_until > now_iso():
        return
    user.is_locked = False
    user.locked_until = None
    user.failed_login_attempts = 0
