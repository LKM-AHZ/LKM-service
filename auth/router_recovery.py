"""密码恢复路由。

端点
---------
POST /auth/recover/check              – 检查可用的恢复方式
POST /auth/recover/phone              – 发送手机验证码
POST /auth/recover/phone/verify       – 通过手机号+验证码重置
POST /auth/recover/email              – 发送邮箱验证码
POST /auth/recover/email/verify       – 通过邮箱+验证码重置
POST /auth/recover/magic-link         – 发送用于密码重置的魔法链接
POST /auth/recover/magic-link/verify  – 通过魔法链接令牌重置
"""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jobs
from app.core.client_ip import client_ip
from app.core.common import ApiResp
from app.core.config import settings
from app.core.err import respond
from auth import service_recovery
from auth.db.session import get_auth_session
from auth.deps import get_email_provider
from auth.limits import (
    GLOBAL_VERIFY_MAX_PER_WINDOW,
    GLOBAL_VERIFY_WINDOW_SECONDS,
    RECOVER_ADMIN_VERIFY_MAX,
    RECOVER_ADMIN_VERIFY_WINDOW,
)
from auth.providers.base import EmailProvider
from auth.schemas import (
    AdminRecoverBeginResponse,
    AdminRecoverVerifyContactResponse,
    AdminRecoverVerifyTOTPResponse,
    MessageResponse,
    RawEmail,
    RecoverCheckResponse,
    RecoverRequires2FAResponse,
)
from auth.service_auth import request_magic_link
from auth.service_verify import (
    check_code_rate_limit,
    create_email_verification,
    create_phone_verification,
)

router = APIRouter(prefix="/auth/recover", tags=["auth-recovery"])

# 请求 Schema（仅限本模块）


class RecoverCheckRequest(BaseModel):
    account: str = Field(..., min_length=1)


class RecoverPhoneRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=20)


class RecoverPhoneVerifyRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=20)
    code: str = Field(..., min_length=6, max_length=6)
    # 非 MFA 账号：本步直接用它完成重置（service_recovery.recover_by_contact 里非空校验）。
    # MFA 账号：本步只开事务返回 txn_id/temp_token，密码改由 /recover/user/complete 接收。
    # 故它既不是「此处不接受」也不是 deprecated——前端 useRecoveryFlow 正是按这个契约传的。
    new_password: str | None = Field(None, min_length=6)


class RecoverEmailRequest(BaseModel):
    email: RawEmail


class RecoverEmailVerifyRequest(BaseModel):
    email: RawEmail
    code: str = Field(..., min_length=6, max_length=6)
    # 语义同 RecoverPhoneVerifyRequest.new_password（非 MFA 在此直接重置，MFA 走 complete 步）
    new_password: str | None = Field(None, min_length=6)


class RecoverMagicLinkRequest(BaseModel):
    email: RawEmail


class RecoverMagicLinkVerifyRequest(BaseModel):
    token: str = Field(..., min_length=1)
    new_password: str | None = Field(None, min_length=6)


@router.post("/check", response_model=ApiResp[RecoverCheckResponse])
@respond
async def recover_check(
    info: RecoverCheckRequest, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    return await service_recovery.check_recovery_methods(db, info.account)


@router.post("/phone", response_model=ApiResp[MessageResponse])
@respond
async def recover_phone(
    info: RecoverPhoneRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    await check_code_rate_limit(f"recover:phone:{info.phone}", max_count=5, window=3600)
    code, _ = await create_phone_verification(db, info.phone, "reset")
    await jobs.send_code("phone", info.phone, code)
    return {"message": "Verification code sent"}


@router.post("/phone/verify", response_model=ApiResp[RecoverRequires2FAResponse])
@respond
async def recover_phone_verify(
    info: RecoverPhoneVerifyRequest, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    await check_code_rate_limit(
        f"recover:phone:verify:{info.phone}", max_count=5, window=3600
    )
    return await service_recovery.recover_by_contact(
        db, info.phone, info.code, info.new_password
    )


@router.post("/email", response_model=ApiResp[MessageResponse])
@respond
async def recover_email(
    info: RecoverEmailRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    await check_code_rate_limit(f"recover:email:{info.email}", max_count=5, window=3600)
    code, _ = await create_email_verification(db, info.email, "reset")
    await jobs.send_code("email", info.email, code)
    return {"message": "Verification code sent"}


@router.post("/email/verify", response_model=ApiResp[RecoverRequires2FAResponse])
@respond
async def recover_email_verify(
    info: RecoverEmailVerifyRequest, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    await check_code_rate_limit(
        f"recover:email:verify:{info.email}", max_count=5, window=3600
    )
    return await service_recovery.recover_by_contact(
        db, info.email, info.code, info.new_password
    )


@router.post("/magic-link", response_model=ApiResp[MessageResponse])
@respond
async def recover_magic_link(
    info: RecoverMagicLinkRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
    email_provider: EmailProvider = Depends(get_email_provider),
) -> dict[str, Any]:
    await request_magic_link(
        db,
        info.email,
        email_provider,
        purpose="reset",
        frontend_url=settings.frontend_callback,
        background_tasks=background_tasks,
    )
    return {"message": "If email exists, magic link sent"}


@router.post("/magic-link/verify", response_model=ApiResp[RecoverRequires2FAResponse])
@respond
async def recover_magic_link_verify(
    info: RecoverMagicLinkVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    # 按 IP 分桶，不用单一全局键：全局键下任何一个客户端把 10 次配额烧光，所有用户的
    # 魔法链接找回都会在同一窗口内被拒（跨租户 DoS）。而令牌是 64 hex（256 bit），
    # 「聚合限流」对暴力猜解本来就没贡献，堵掉这个 DoS 面是纯赚。
    await check_code_rate_limit(
        f"recover:magic-link:verify:ip:{client_ip(request)}",
        max_count=GLOBAL_VERIFY_MAX_PER_WINDOW,
        window=GLOBAL_VERIFY_WINDOW_SECONDS,
    )
    return await service_recovery.recover_by_magic_link(
        db, info.token, info.new_password
    )


class RecoverUserVerifyTOTPRequest(BaseModel):
    txn_id: str = Field(..., min_length=1)
    temp_token: str = Field(..., min_length=1)


class RecoverUserCompleteRequest(BaseModel):
    txn_id: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


async def _limit_recovery_step(action: str, request: Request) -> None:
    """恢复流程各核验步的按 IP 限流。

    txn_id/temp_token 都是高熵值、猜不动，这里主要是与同模块其它核验步（verify-contact、
    magic-link verify）保持一致的 defense-in-depth，并给这些低频端点一个成本上界；
    用 IP 而非 txn_id 作桶：后者由客户端给出，换个 txn_id 就换一个空桶，限不住。
    """
    await check_code_rate_limit(
        f"recover:{action}:ip:{client_ip(request)}",
        max_count=GLOBAL_VERIFY_MAX_PER_WINDOW,
        window=GLOBAL_VERIFY_WINDOW_SECONDS,
    )


@router.post("/verify-totp", response_model=ApiResp[AdminRecoverVerifyTOTPResponse])
@respond
async def recover_user_verify_totp(
    info: RecoverUserVerifyTOTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """确认用户恢复事务的 2FA。需要用户在完成 2FA 后从 /auth/2fa/verify 获取的 temp_token。"""
    await _limit_recovery_step("user-verify-totp", request)
    return await service_recovery.recover_admin_verify_totp(
        db, info.txn_id, info.temp_token
    )


@router.post("/complete", response_model=ApiResp[MessageResponse])
@respond
async def recover_user_complete(
    info: RecoverUserCompleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用新密码完成用户恢复。需要已验证的联系方式+2FA。"""
    await _limit_recovery_step("user-complete", request)
    return await service_recovery.recover_user_complete(
        db, info.txn_id, info.new_password
    )


class RecoverAdminRequest(BaseModel):
    contact: str = Field(..., min_length=1, description="Email or phone of the admin")


class RecoverAdminVerifyContactRequest(BaseModel):
    txn_id: str = Field(..., min_length=1)
    code: str = Field(..., min_length=6, max_length=6)


class RecoverAdminVerifyTOTPRequest(BaseModel):
    txn_id: str = Field(..., min_length=1)
    temp_token: str = Field(..., min_length=1)


class RecoverAdminCompleteRequest(BaseModel):
    txn_id: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


@router.post("/admin/begin", response_model=ApiResp[AdminRecoverBeginResponse])
@respond
async def recover_admin_begin(
    info: RecoverAdminRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """第1步：发起管理员恢复。服务层负责生成并发送验证码。"""
    return await service_recovery.recover_admin_begin(
        db, info.contact, background_tasks=background_tasks
    )


@router.post(
    "/admin/verify-contact", response_model=ApiResp[AdminRecoverVerifyContactResponse]
)
@respond
async def recover_admin_verify_contact(
    info: RecoverAdminVerifyContactRequest, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    """第2步：验证联系方式验证码。返回用于 2FA 的 temp_token。"""
    await check_code_rate_limit(
        f"recover:admin:verify-contact:{info.txn_id}",
        max_count=RECOVER_ADMIN_VERIFY_MAX,
        window=RECOVER_ADMIN_VERIFY_WINDOW,
    )
    return await service_recovery.recover_admin_verify_contact(
        db, info.txn_id, info.code
    )


@router.post(
    "/admin/verify-totp", response_model=ApiResp[AdminRecoverVerifyTOTPResponse]
)
@respond
async def recover_admin_verify_totp(
    info: RecoverAdminVerifyTOTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """第3步：确认 2FA 已完成。需要从 /auth/2fa/verify 获取的 temp_token。"""
    await _limit_recovery_step("admin-verify-totp", request)
    return await service_recovery.recover_admin_verify_totp(
        db, info.txn_id, info.temp_token
    )


@router.post("/admin/complete", response_model=ApiResp[MessageResponse])
@respond
async def recover_admin_complete(
    info: RecoverAdminCompleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """第4步：设置新密码。需要前面所有步骤已完成。"""
    await _limit_recovery_step("admin-complete", request)
    return await service_recovery.recover_admin_complete(
        db, info.txn_id, info.new_password
    )
