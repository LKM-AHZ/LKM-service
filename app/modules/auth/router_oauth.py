"""Github OAuth 路由 – 登录重定向、回调、绑定。"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import respond
from app.db.session import get_session
from app.modules.auth import service_oauth
from app.modules.auth.deps import CurrentUser, get_current_user
from app.modules.auth.schemas import (
    AuthTokenData,
    MessageResponse,
    OAuthRedirectResponse,
)
from app.modules.common import ApiResp

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


@router.get("/github/login")
async def github_login(db: AsyncSession = Depends(get_session)) -> RedirectResponse:
    """将用户重定向到 Github OAuth 授权页面。"""
    url = await service_oauth.get_github_auth_url(db, purpose="login")
    return RedirectResponse(url=url)


@router.get("/github/callback", response_model=ApiResp[AuthTokenData])
@respond
async def github_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """处理 Github OAuth 回调。验证状态令牌，交换授权码，查找或创建用户，并返回认证令牌。"""
    return await service_oauth.handle_github_callback(db, code, state)


@router.post("/github/login/redirect", response_model=ApiResp[OAuthRedirectResponse])
@respond
async def github_bind_redirect(_cur: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_session)) -> dict[str, str]:
    """返回用于绑定的 OAuth 授权 URL（从 JS 客户端调用）。"""
    url = await service_oauth.get_github_auth_url(db, purpose="bind")
    return {"url": url}


@router.get("/github/bind-callback", response_model=ApiResp[MessageResponse])
@respond
async def github_bind_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """处理用于账号绑定的 Github OAuth 回调。

    归属用户由 OAuth state 记录携带（``bind_github`` 内部从 state 取 user_id），
    回调无需 JWT 鉴权，故不注入 CurrentUser。
    """
    return await service_oauth.bind_github(db, code, state)
