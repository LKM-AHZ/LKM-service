"""Passkey (WebAuthn) HTTP 端点。

POST   /auth/passkey/register/begin     RequireLevel("normal")  开始 Passkey 注册
POST   /auth/passkey/register/complete  RequireLevel("normal")  完成 Passkey 注册
POST   /auth/passkey/login/begin        public                  开始 Passkey 登录
POST   /auth/passkey/login/complete     public                  完成 Passkey 登录
GET    /auth/passkey/credentials        get_current_user        列出 Passkey 凭据
DELETE /auth/passkey/{cred_id}          get_current_user        删除 Passkey 凭据
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import respond
from app.db.auth_session import get_auth_session
from app.modules.auth import service_passkey
from app.modules.auth.deps import CurrentUser, get_current_user, require_2fa
from app.modules.auth.schemas import (
    AuthTokenData,
    MessageResponse,
    PasskeyCredentialItem,
    PasskeyLoginCompleteRequest,
    PasskeyLoginOptionsResponse,
    PasskeyRegisterCompleteRequest,
    PasskeyRegisterCompleteResponse,
    PasskeyRegistrationOptionsResponse,
)

router = APIRouter(prefix="/auth/passkey", tags=["auth-passkey"])


@router.post(
    "/register/begin", response_model=ApiResp[PasskeyRegistrationOptionsResponse]
)
@respond
async def begin_passkey_registration(
    cur: CurrentUser = Depends(get_current_user),
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
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用客户端传来的凭据完成 Passkey 注册。"""
    return await service_passkey.complete_passkey_registration(
        db, cur.id, body.model_dump()
    )


@router.post("/login/begin", response_model=ApiResp[PasskeyLoginOptionsResponse])
@respond
async def begin_passkey_login(
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """开始 Passkey 登录。返回 PublicKeyCredentialRequestOptions。"""
    return await service_passkey.begin_passkey_login(db)


@router.post("/login/complete", response_model=ApiResp[AuthTokenData])
@respond
async def complete_passkey_login(
    body: PasskeyLoginCompleteRequest,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用客户端传来的凭据完成 Passkey 登录。"""
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
