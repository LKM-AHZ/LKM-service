"""
用户/通知能力端口：渠道与 provider、用户运维操作、user_dim 对账。
实现由 ``auth.ports_impl`` 绑定（实现内部惰性取 auth 属性，保证既有 monkeypatch 生效）。
所有函数都是薄转发——core 不持任何 auth 实现。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from core.ports.registry import get

# ---------------------------------------------------------------- 渠道 / provider


def get_channel(channel_key: str) -> Any:
    """取发送渠道（短信/邮件等）；降级发送路径用。"""
    return get("users").get_channel(channel_key)


def get_email_provider() -> Any:
    """取邮件 provider（发验证码/魔法链接）。"""
    return get("users").get_email_provider()


def get_sms_provider() -> Any:
    """取短信 provider。"""
    return get("users").get_sms_provider()


# ---------------------------------------------------------------- 用户运维


async def ensure_demo_user(
    *,
    username: str,
    nickname: str,
    email: str | None = None,
    account_level: str = "local",
) -> Any:
    """幂等确保一个 auth realm 演示用户存在，返回其 uuid。"""
    return await get("users").ensure_demo_user(
        username=username, nickname=nickname, email=email, account_level=account_level
    )


async def mint_bot_sso_ticket(
    user_id: Any, account_level: str = "admin"
) -> dict[str, Any]:
    """铸一次性 bot 面板 SSO 票据（fail-closed：不可达即抛 BizError(UNAVAILABLE)）。"""
    return await get("users").mint_bot_sso_ticket(
        user_id, account_level=account_level
    )


async def verify_password(
    db: Any, username: str, password: str
) -> dict[str, Any] | None:
    """校验一组 Basic 凭证，返回 ``{"user_id", "username"}``；权威否答返回 None。

    融合形态（seam 关闭）下由 auth 实现用传入的 ``db`` 就地查 User 行；拆库形态走
    auth 内部验密端点。缝不可用抛 ``BizError(UNAVAILABLE)``（fail-closed）。
    """
    return await get("users").verify_password(db, username, password)


async def verifypwd(plain: str, hashed: str) -> bool:
    """口令校验（Argon2，实现里下放线程池）。"""
    return await get("users").verifypwd(plain, hashed)


async def hashpwd(plain: str) -> str:
    """口令哈希（Argon2，实现里下放线程池）。"""
    return await get("users").hashpwd(plain)


# ---------------------------------------------------------------- user_dim ETL


async def open_session_pair() -> Any:
    """开跨 realm 双会话（源=auth 只读 / 目标=业务可写）。"""
    return await get("users").open_session_pair()


async def reconcile_user_dim_periodic() -> int:
    """一拍周期对账（自开会话 + Redis 锁 + commit）。"""
    return await get("users").reconcile_user_dim_periodic()


async def reconcile_user_dim_incremental(src: Any, tgt: Any, *, window: int) -> int:
    """单拍增量对账（src/tgt 由 :func:`open_session_pair` 提供）。"""
    return await get("users").reconcile_user_dim_incremental(src, tgt, window=window)


async def sync_dim_for_ids(src: Any, tgt: Any, user_ids: list[int]) -> int:
    """显式 id 批量回填 user_dim。"""
    return await get("users").sync_dim_for_ids(src, tgt, user_ids)


# ---------------------------------------------------------------- 业务侧升权


async def grant_exam_unlock_from_business(
    db: Any,
    user_id: uuid.UUID,
    *,
    unlock_level: str | None,
    unlock_role: str | None,
) -> None:
    """业务侧授予考试解锁（升 account_level/role，auth 域权威写）。"""
    await get("users").grant_exam_unlock_from_business(
        db, user_id, unlock_level=unlock_level, unlock_role=unlock_role
    )


async def grant_incubation_from_business(db: Any, applicant_id: uuid.UUID) -> None:
    """业务侧纳入成员升级（auth 域权威写）。"""
    await get("users").grant_incubation_from_business(db, applicant_id)


#: 供实现方参考的回调类型别名（测试桩可用普通函数替代）。
SessionPairRunner = Callable[[Any, Any], Awaitable[int]]
