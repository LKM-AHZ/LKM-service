"""挑战-因子轮转的 HTTP 面。

轮转语义见 :mod:`auth.challenge`；这里只做参数校验、速率限制与响应包装。

设备标识统一走 ``X-Device-Id`` 请求头（见 :mod:`auth.deps`）：所有挑战端点都要它，
放 body 会让 GET 读状态没有位置，放 query 又得和 body 混着记。

典型流程（密码登录，2 RTT）：

1. ``POST /auth/challenge``                      → 挑战 + ``factors:[{kind:password}]``
2. ``POST /auth/challenge/{id}/factor``          → 令牌，或下一批因子

验证码登录多一步 ``POST /auth/challenge/{id}/request`` 触发发码；通行密钥走
``/{id}/passkey/start`` + ``/{id}/passkey/complete``。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from auth import challenge as challenge_service
from auth.channels import channel_for
from auth.db.session import get_auth_session
from auth.deps import require_device_id
from auth.schemas import (
    ChallengeCreate,
    ChallengeFactorSubmit,
    ChallengePasskeyComplete,
    ChallengePasskeyStart,
    ChallengeState,
    MessageResponse,
)
from auth.service_verify import check_code_rate_limit
from core.client_ip import client_ip
from core.common import ApiResp
from core.err import respond

router = APIRouter(prefix="/auth/challenge", tags=["auth-challenge"])

#: 建挑战不校验凭据，是纯写入端点，按 IP 限速防止刷表。
_CREATE_MAX = 30
_CREATE_WINDOW_SECONDS = 600


@router.post("", response_model=ApiResp[ChallengeState])
@respond
async def create_challenge_route(
    payload: ChallengeCreate,
    request: Request,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """建挑战。不查账号是否存在——那一步留到提交因子。"""
    await check_code_rate_limit(
        f"challenge:create:{client_ip(request)}",
        max_count=_CREATE_MAX,
        window=_CREATE_WINDOW_SECONDS,
    )
    challenge = await challenge_service.create_challenge(
        db,
        account=payload.account,
        device_id=device,
        primary_factor=payload.primary_factor,
    )
    return challenge_service.state_payload(challenge)


@router.get("/{challenge_id}", response_model=ApiResp[ChallengeState])
@respond
async def get_challenge_route(
    challenge_id: uuid.UUID,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """读挑战当前状态。客户端重连或回到登录页时用它恢复进度。"""
    challenge = await challenge_service.load_challenge(db, challenge_id, device)
    return challenge_service.state_payload(challenge)


@router.post("/{challenge_id}/factor", response_model=ApiResp[dict[str, Any]])
@respond
async def submit_factor_route(
    challenge_id: uuid.UUID,
    payload: ChallengeFactorSubmit,
    request: Request,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """提交一个因子：返回下一状态，或完成态（含令牌）。

    响应形状随进度变化：``{"status":"pending","factors":[...]}`` 或
    ``{"status":"complete","access_token":...}``。
    """
    challenge = await challenge_service.load_challenge(db, challenge_id, device)
    return await challenge_service.submit_factor(
        db,
        challenge,
        kind=payload.kind,
        password=payload.password,
        code=payload.code,
        recovery_code=payload.recovery_code,
        ip_address=client_ip(request),
    )


@router.post(
    "/{challenge_id}/passkey/start", response_model=ApiResp[dict[str, Any]]
)
@respond
async def start_passkey_factor_route(
    challenge_id: uuid.UUID,
    payload: ChallengePasskeyStart,
    request: Request,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """生成 passkey 因子的断言选项（PublicKeyCredentialRequestOptions）。

    与 ``/auth/passkey/login/begin`` 同款限流：这是一条公开写库面。
    """
    await check_code_rate_limit(
        f"challenge:passkey:start:{client_ip(request)}", max_count=20, window=60
    )
    challenge = await challenge_service.load_challenge(db, challenge_id, device)
    return await challenge_service.prepare_passkey_factor(db, challenge)


@router.post(
    "/{challenge_id}/passkey/complete", response_model=ApiResp[dict[str, Any]]
)
@respond
async def complete_passkey_factor_route(
    challenge_id: uuid.UUID,
    payload: ChallengePasskeyComplete,
    request: Request,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """提交 passkey 断言：完成挑战并返回令牌。"""
    await check_code_rate_limit(
        f"challenge:passkey:complete:{client_ip(request)}", max_count=10, window=60
    )
    challenge = await challenge_service.load_challenge(db, challenge_id, device)
    return await challenge_service.complete_passkey(db, challenge, payload.model_dump())


@router.post("/{challenge_id}/request", response_model=ApiResp[MessageResponse])
@respond
async def request_factor_code_route(
    challenge_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """为 ``code`` 因子发验证码。账号不存在时返回同一文案。"""
    challenge = await challenge_service.load_challenge(db, challenge_id, device)
    issued = await challenge_service.issue_login_code(db, challenge)
    if issued is not None:
        contact, code = issued
        background_tasks.add_task(channel_for(contact).send_code, contact, code)
    return {"message": "If account exists, verification code sent"}


@router.post("/{challenge_id}/cancel", response_model=ApiResp[MessageResponse])
@respond
async def cancel_challenge_route(
    challenge_id: uuid.UUID,
    device: str = Depends(require_device_id),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """作废挑战（用户主动放弃或切换登录方式）。"""
    challenge = await challenge_service.load_challenge(db, challenge_id, device)
    await challenge_service.cancel_challenge(db, challenge)
    return {"message": "Challenge cancelled"}
