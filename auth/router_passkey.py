"""Passkey (WebAuthn) HTTP 端点。

POST   /auth/passkey/register/begin     RequireLevel("normal")  开始 Passkey 注册
POST   /auth/passkey/register/complete  RequireLevel("normal")  完成 Passkey 注册
POST   /auth/passkey/login/begin        public                  开始 Passkey 登录
POST   /auth/passkey/login/complete     public                  完成 Passkey 登录
GET    /auth/passkey/credentials        get_current_user        列出 Passkey 凭据
DELETE /auth/passkey/{cred_id}          require_2fa             删除 Passkey 凭据
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ip import client_ip
from app.core.common import ApiResp
from app.core.err import respond
from auth import service_passkey
from auth.db.session import get_auth_session
from auth.deps import CurrentUser, RequireLevel, get_current_user, require_2fa
from auth.schemas import (
    AuthTokenData,
    MessageResponse,
    PasskeyCredentialItem,
    PasskeyLoginCompleteRequest,
    PasskeyLoginOptionsResponse,
    PasskeyRegisterCompleteRequest,
    PasskeyRegisterCompleteResponse,
    PasskeyRegistrationOptionsResponse,
)
from auth.service_verify import check_code_rate_limit

router = APIRouter(prefix="/auth/passkey", tags=["auth-passkey"])


@router.post(
    "/register/begin", response_model=ApiResp[PasskeyRegistrationOptionsResponse]
)
@respond
async def begin_passkey_registration(
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """开始 Passkey 注册。返回 PublicKeyCredentialCreationOptions。"""
    return await service_passkey.begin_passkey_registration(db, cur.id)


@router.post(
    "/register/complete", response_model=ApiResp[PasskeyRegisterCompleteResponse]
)
@respond
async def complete_passkey_registration(
    body: PasskeyRegisterCompleteRequest,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用客户端传来的凭据完成 Passkey 注册。"""
    return await service_passkey.complete_passkey_registration(
        db, cur.id, body.model_dump()
    )


@router.post("/login/begin", response_model=ApiResp[PasskeyLoginOptionsResponse])
@respond
async def begin_passkey_login(
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """开始 Passkey 登录。返回 PublicKeyCredentialRequestOptions。

    公开端点：每次调用都会落一行 PasskeyChallenge（5min TTL），不设 IP 限流等于开放写库面。
    """
    await check_code_rate_limit(
        f"passkey:login:begin:{client_ip(request)}", max_count=20, window=60
    )
    return await service_passkey.begin_passkey_login(db)


@router.post("/login/complete", response_model=ApiResp[AuthTokenData])
@respond
async def complete_passkey_login(
    body: PasskeyLoginCompleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用客户端传来的凭据完成 Passkey 登录。

    公开端点且允许反复尝试签名验证：不设 IP 限流等于开放未认证爆破面（其余
    验证码/密码登录路径均有 check_code_rate_limit 同款保护）。
    """
    await check_code_rate_limit(
        f"passkey:login:complete:{client_ip(request)}", max_count=10, window=60
    )
    return await service_passkey.complete_passkey_login(db, body.model_dump())


@router.get("/credentials", response_model=ApiResp[list[PasskeyCredentialItem]])
@respond
async def list_credentials(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> list[dict[str, Any]]:
    """列出当前用户的所有 Passkey 凭据。"""
    return await service_passkey.list_credentials(db, cur.id)


@router.delete("/{cred_id}", response_model=ApiResp[MessageResponse])
@respond
async def delete_credential(
    cred_id: uuid.UUID,
    cur: CurrentUser = require_2fa,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """通过数据库 ID 删除一个 Passkey 凭据。"""
    return await service_passkey.delete_credential(db, cur.id, cred_id)
