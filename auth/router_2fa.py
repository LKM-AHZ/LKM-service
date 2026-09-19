"""
2FA (TOTP) HTTP 端点。
POST   /auth/2fa/setup/begin     RequireLevel("normal")  开始 TOTP 设置
POST   /auth/2fa/setup/temp      temp_token 认证         开始 TOTP 设置（管理员强制设置）
POST   /auth/2fa/setup/complete  RequireLevel("normal")  完成 TOTP 设置
POST   /auth/2fa/verify          public (temp_token)     登录时验证 2FA
DELETE /auth/2fa                  RequireLevel("normal")  禁用 2FA
"""

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import BizError, respond
from app.db.base import expires_at, now_iso
from app.db.repo import consume_once, get_or_raise
from auth import security, service_2fa
from auth.db.session import get_auth_session
from auth.deps import CurrentUser, RequireLevel, get_current_user
from auth.errors import AuthErr
from auth.limits import (
    GLOBAL_VERIFY_MAX_PER_WINDOW,
    GLOBAL_VERIFY_WINDOW_SECONDS,
)
from auth.models import SetupTransaction, User
from auth.schemas import (
    TOTPConfirmResponse,
    TOTPDisableRequest,
    TOTPDisableResponse,
    TOTPSetupBeginData,
    TOTPSetupCompleteData,
    TOTPSetupCompleteRequest,
    TOTPSetupCompleteTempData,
    TOTPStatusData,
    TOTPVerifyRequest,
    TOTPVerifyResponse,
)
from auth.service_auth import issue_session_tokens
from auth.service_verify import check_code_rate_limit

router = APIRouter(prefix="/auth/2fa", tags=["auth-2fa"])


def _decode_setup_temp_token(temp_token: str) -> tuple[str, uuid.UUID]:
    """解码并验证 setup 临时令牌，返回 (token_hash, user_id)。

    ``security.create_temp_token`` 把 user_id 编为 ``str(user_id)``（JSON 无 uuid 类型），
    此处还原为 ``uuid.UUID``；缺失/畸形一律 TOKEN_INVALID。
    """
    try:
        payload = security.decode_temp_token(temp_token)
    except Exception as exc:
        raise BizError(AuthErr.TOKEN_INVALID) from exc
    if payload.get("purpose") != "setup":
        raise BizError(AuthErr.TOKEN_INVALID, "Not a setup token")
    raw_user_id = payload.get("user_id")
    if not raw_user_id:
        raise BizError(AuthErr.TOKEN_INVALID)
    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (AttributeError, TypeError, ValueError):
        raise BizError(AuthErr.TOKEN_INVALID) from None
    token_hash = hashlib.sha256(temp_token.encode()).hexdigest()
    return token_hash, user_id


@router.post("/setup/begin", response_model=ApiResp[TOTPSetupBeginData])
@respond
async def setup_2fa_begin(
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """开始 TOTP 设置。返回密钥和二维码 URI。"""
    result = await service_2fa.setup_2fa_begin(db, cur.id)
    return result


@router.post("/setup/temp", response_model=ApiResp[TOTPSetupBeginData])
@respond
async def setup_2fa_temp(
    temp_token: str,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用登录时获得的临时令牌开始 TOTP 设置（管理员强制设置）。"""
    token_hash, user_id = _decode_setup_temp_token(temp_token)

    # 原子性地声明设置令牌 — 只有一个 begin 调用会成功
    try:
        db.add(
            SetupTransaction(
                token_hash=token_hash,
                user_id=user_id,
                consumed=False,
                expires_at=expires_at(minutes=10),
            )
        )
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise BizError(AuthErr.TOKEN_INVALID, "Setup token already used") from None

    return await service_2fa.setup_2fa_begin(db, user_id)


@router.post("/setup/complete/temp", response_model=ApiResp[TOTPSetupCompleteTempData])
@respond
async def setup_2fa_complete_temp(
    temp_token: str,
    code: str,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用临时令牌完成 TOTP 设置（管理员强制设置路径）。"""
    token_hash, user_id = _decode_setup_temp_token(temp_token)

    # 原子性地消耗设置事务
    if not await consume_once(
        db,
        SetupTransaction,
        {"consumed": True},
        SetupTransaction.token_hash == token_hash,
        SetupTransaction.consumed.is_(False),
        SetupTransaction.expires_at > now_iso(),
    ):
        raise BizError(
            AuthErr.TOKEN_INVALID, "Setup token not found, already used, or expired"
        )

    txn = (
        (
            await db.execute(
                select(SetupTransaction).where(
                    SetupTransaction.token_hash == token_hash
                )
            )
        )
        .scalars()
        .first()
    )
    if not txn or txn.user_id != user_id:
        raise BizError(AuthErr.TOKEN_INVALID)

    result_dict = await service_2fa.setup_2fa_complete(db, user_id, code)
    user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if user:
        (
            result_dict["access_token"],
            result_dict["refresh_token"],
        ) = await issue_session_tokens(db, user, mfa_verified=True)
    return result_dict


@router.post("/setup/complete", response_model=ApiResp[TOTPSetupCompleteData])
@respond
async def setup_2fa_complete(
    body: TOTPSetupCompleteRequest,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """通过验证 TOTP 码完成 TOTP 设置。返回恢复码。"""
    result = await service_2fa.setup_2fa_complete(db, cur.id, body.code)
    return result


@router.post("/setup/confirm", response_model=ApiResp[TOTPConfirmResponse])
@respond
async def confirm_recovery_codes(
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """确认用户已保存其恢复码。"""
    return await service_2fa.confirm_recovery_codes_saved(db, cur.id)


@router.post("/verify", response_model=ApiResp[TOTPVerifyResponse])
@respond
async def verify_2fa(
    body: TOTPVerifyRequest,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """在登录时使用临时令牌和 TOTP / 恢复码验证 2FA。"""
    # 按用户分桶（而非全站共享）：防止单个攻击者刷错耗尽共享额度封锁所有用户的 2FA
    # 登录。临时令牌可解码出 user_id；无法解码（本就会 400）用低熵兜底桶，避免泄漏失败面。
    try:
        _raw_uid = security.decode_temp_token(body.temp_token).get("user_id")
        _v_uid = str(_raw_uid) if _raw_uid else "0"
    except Exception:
        _v_uid = "0"
    await check_code_rate_limit(
        f"2fa:verify:user:{_v_uid}",
        max_count=GLOBAL_VERIFY_MAX_PER_WINDOW,
        window=GLOBAL_VERIFY_WINDOW_SECONDS,
    )
    result = await service_2fa.verify_2fa(
        db,
        temp_token=body.temp_token,
        code=body.code,
        recovery_code=body.recovery_code,
        trust_device=body.trust_device,
    )
    return result


@router.delete("", response_model=ApiResp[TOTPDisableResponse])
@respond
async def disable_2fa(
    body: TOTPDisableRequest,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """为当前用户禁用 2FA。需要有效的 TOTP 码或恢复码。"""
    result = await service_2fa.disable_2fa(
        db, cur.id, code=body.code, recovery_code=body.recovery_code
    )
    return result


@router.get("/status", response_model=ApiResp[TOTPStatusData])
@respond
async def get_2fa_status(
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """返回当前用户 2FA 是否已开启。"""
    totp = await service_2fa.get_enabled_totp(db, cur.id)
    return {"enabled": totp is not None}


@router.post("/step-up", response_model=ApiResp[TOTPVerifyResponse])
@respond
async def step_up_2fa(
    body: TOTPDisableRequest,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """前台危险操作 step-up：验证当前已登录用户的第二因素（TOTP 或恢复码），通过后签发带 2FA 信任的 access token。

    信任窗口 1 小时（auth/deps.MFA_TRUST_SECONDS），期间前台删除类危险端点
    （require_2fa）不再重复要求。未通过则不更新信任，仅抛 TOTP/RECOVERY 相关错误。
    返回新 access/refresh token（`mfa`/`mfa_at` 标记已编入 access token）。
    """
    await check_code_rate_limit(
        f"2fa:stepup:user:{cur.id}",
        max_count=GLOBAL_VERIFY_MAX_PER_WINDOW,
        window=GLOBAL_VERIFY_WINDOW_SECONDS,
    )
    await service_2fa.verify_second_factor(
        db, cur.id, code=body.code, recovery_code=body.recovery_code
    )

    user = await get_or_raise(db, User, AuthErr.USER_NOT_FOUND, User.id == cur.id)
    access_token, raw_refresh = await issue_session_tokens(db, user, mfa_verified=True)
    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "user_id": str(user.id),  # 主键已 UUID；JSON 无 uuid 类型，按字符串出 wire
        "account_level": str(user.account_level),
        "mfa_verified": True,
    }
