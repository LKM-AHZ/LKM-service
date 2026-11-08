"""挑战-因子轮转的登录编排。

一次登录 = 一个服务端持有状态的「挑战」。客户端建挑战拿到**第一因子**，提交后要么
拿到令牌（结束），要么拿到下一批因子；新增因子只需在本模块登记，客户端不用改版。

与 Solian stargate 的三处刻意差异：

1. **不绑定 IP**。桌面端会换网络（Wi-Fi → 热点），绑 IP 的挑战在换网后会被判成
   「挑战不存在」。这里绑定 ``device_id`` 的哈希，网络变化不影响，换机器仍然拦。
2. **因子渐进披露**。建挑战只返回第一因子；密码/验证码通过后才返回后续因子。
   一次性列出「该账号有 passkey、有 TOTP」就是一个账号枚举预言机。
3. **验证码复用原通道**。发码仍走 :func:`auth.channels` 的同一套逻辑，含「账号不存在
   也返回同一文案」的既有防护，不在挑战层另起一套。

暂不支持而在挑战内显式拒绝的步骤：管理员首次强制设置 TOTP（响应带 ``temp_token`` +
``setup_required``）。那条路径继续走 ``/auth/2fa/setup/complete/temp``，挑战层报
``CHALLENGE_UNSUPPORTED_STEP``，避免把 temp_token 落库。
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from typing import Any

from auth.channels import channel_for
from auth.errors import AuthErr
from auth.models import AuthChallenge
from auth.schemas import UserLoginPassword
from auth.service_auth import login_code, login_password
from auth.service_verify import check_code_rate_limit
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError

logger = logging.getLogger(__name__)

FACTOR_PASSWORD = "password"
FACTOR_CODE = "code"
FACTOR_TOTP = "totp"

#: 挑战有效期。用户停在这一屏去翻邮箱的时间要算进来，但也不能太长。
CHALLENGE_TTL_MINUTES = 5

#: 单挑战允许的因子提交次数上限（含失败）。凭据本身的失败计数与锁定仍由
#: ``login_password`` 内部那套（5 次锁 15 分钟）负责，这里只是兜底重复提交。
MAX_ATTEMPTS = 5

_DEVICE_PREFIX = "lkm-challenge:"


def fingerprint(device_id: str) -> str:
    """设备标识的哈希。挑战只认哈希，不存原始 device_id。"""
    return hashlib.sha256((_DEVICE_PREFIX + device_id).encode()).hexdigest()


async def create_challenge(
    db: DbSession,
    *,
    account: str,
    device_id: str,
    purpose: str = "login",
    primary_factor: str = FACTOR_PASSWORD,
) -> AuthChallenge:
    """建挑战。**不查用户**：账号是否存在要到提交因子时才体现。"""
    if primary_factor not in (FACTOR_PASSWORD, FACTOR_CODE):
        raise BizError(AuthErr.CHALLENGE_STEP_INVALID)

    now = now_iso()
    challenge = AuthChallenge(
        account=account.strip(),
        purpose=purpose,
        device_hash=fingerprint(device_id),
        status="pending",
        steps_done=[],
        steps_left=[primary_factor],
        expires_at=now + datetime.timedelta(minutes=CHALLENGE_TTL_MINUTES),
    )
    db.add(challenge)
    await db.commit()
    await db.refresh(challenge)
    return challenge


async def load_challenge(
    db: DbSession,
    challenge_id: uuid.UUID,
    device_id: str,
) -> AuthChallenge:
    """按 id + 设备取挑战，并核对状态、归属与有效期。

    设备不匹配与不存在返回**同一个** ``CHALLENGE_NOT_FOUND``：区分开就等于告诉
    攻击者「这个 id 是真的，只是设备不对」。
    """
    challenge = await db.get(AuthChallenge, challenge_id)
    if challenge is None or challenge.device_hash != fingerprint(device_id):
        raise BizError(AuthErr.CHALLENGE_NOT_FOUND)
    if challenge.consumed or challenge.status == "complete":
        raise BizError(AuthErr.CHALLENGE_CONSUMED)
    if challenge.status == "cancelled":
        raise BizError(AuthErr.CHALLENGE_NOT_FOUND)
    if challenge.expires_at <= now_iso():
        raise BizError(AuthErr.CHALLENGE_EXPIRED)
    return challenge


async def submit_factor(
    db: DbSession,
    challenge: AuthChallenge,
    *,
    kind: str,
    password: str | None = None,
    code: str | None = None,
    ip_address: str = "",
) -> dict[str, Any]:
    """提交一个因子：返回下一状态，或在最后一个因子后返回令牌。

    凭据校验完全复用既有服务，速率限制、失败计数、锁定、准入、审计都在原处生效。
    """
    expected = challenge.steps_left[0] if challenge.steps_left else None
    if expected is None:
        raise BizError(AuthErr.CHALLENGE_CONSUMED)
    if kind != expected:
        raise BizError(AuthErr.CHALLENGE_STEP_INVALID)

    challenge.attempts += 1
    if challenge.attempts > MAX_ATTEMPTS:
        await _consume(db, challenge, status="cancelled")
        raise BizError(AuthErr.CHALLENGE_TOO_MANY_ATTEMPTS)

    if kind == FACTOR_PASSWORD:
        data = await login_password(
            db,
            UserLoginPassword(
                account=challenge.account,
                password=password or "",
            ),
            ip_address=ip_address,
        )
    elif kind == FACTOR_CODE:
        data = await login_code(db, challenge.account, code or "")
    else:  # pragma: no cover - 由 load 之前的 kind 校验拦住
        raise BizError(AuthErr.CHALLENGE_STEP_INVALID)

    return await _advance(db, challenge, factor=kind, data=data)


async def issue_login_code(
    db: DbSession, challenge: AuthChallenge
) -> tuple[str, str] | None:
    """为 ``code`` 因子发验证码；账号不存在时返回 None（调用方仍回同一文案）。"""
    await check_code_rate_limit(
        f"challenge:code:{challenge.device_hash}", max_count=5, window=3600
    )
    contact = challenge.account
    channel = channel_for(contact)
    user = await channel.find_user(db, contact)
    if user is None or user.account_level == "local":
        return None
    code, _ = await channel.create_verification(db, contact, "login")
    return contact, code


async def cancel_challenge(db: DbSession, challenge: AuthChallenge) -> None:
    await _consume(db, challenge, status="cancelled")


def state_payload(challenge: AuthChallenge) -> dict[str, Any]:
    """客户端渲染用的挑战快照。只暴露下一步及之后的因子种类。"""
    return {
        "id": str(challenge.id),
        "status": challenge.status,
        "purpose": challenge.purpose,
        "factors": [{"kind": kind} for kind in challenge.steps_left],
        "expires_at": challenge.expires_at.isoformat(),
    }


async def _advance(
    db: DbSession,
    challenge: AuthChallenge,
    *,
    factor: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """把一次成功的凭据校验结果落成挑战的下一态或完成态。"""
    # 管理员首次强制设置 TOTP：响应里带的是 temp_token 而不是会话令牌。
    if data.get("setup_required") or data.get("temp_token"):
        await _consume(db, challenge, status="cancelled")
        raise BizError(AuthErr.CHALLENGE_UNSUPPORTED_STEP)

    access_token = data.get("access_token")
    if not access_token:
        # 准入待审/被拒等：既没令牌也没后续因子，按步骤失败结束挑战。
        await _consume(db, challenge, status="cancelled")
        raise BizError(AuthErr.CHALLENGE_UNSUPPORTED_STEP)

    done = [*challenge.steps_done, factor]

    if data.get("requires_2fa"):
        challenge.steps_done = done
        challenge.steps_left = [FACTOR_TOTP]
        await db.commit()
        await db.refresh(challenge)
        return state_payload(challenge)

    challenge.steps_done = done
    challenge.steps_left = []
    challenge.status = "complete"
    challenge.consumed = True
    raw_user_id = data.get("user_id")
    if raw_user_id is not None:
        challenge.user_id = uuid.UUID(str(raw_user_id))
    await db.commit()
    await db.refresh(challenge)

    return {
        "status": "complete",
        "access_token": access_token,
        "refresh_token": data.get("refresh_token"),
        "user_id": str(raw_user_id) if raw_user_id is not None else None,
        "account_level": data.get("account_level"),
    }


async def _consume(
    db: DbSession, challenge: AuthChallenge, *, status: str
) -> None:
    challenge.status = status
    challenge.consumed = True
    challenge.steps_left = []
    await db.commit()
