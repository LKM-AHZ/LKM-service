"""Github OAuth 路由 – 登录重定向、回调、绑定。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import BizError, respond
from auth import service_oauth
from auth.db.session import get_auth_session
from auth.deps import CurrentUser, get_current_user
from auth.errors import AuthErr
from auth.schemas import (
    AuthTokenData,
    MessageResponse,
    OAuthRedirectResponse,
)

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


def _guard_oauth_query(
    code: str | None,
    state: str | None,
    error: str | None,
    error_description: str | None,
) -> tuple[str, str]:
    """把 OAuth 回调 query 归一成 ``(code, state)``，异常情况翻成可读的领域错误。

    用户拒绝授权时 GitHub 只带 ``error``/``error_description`` 回来（没有 code/state），
    若把 code/state 声明成必填，这种**合法回调**会被 FastAPI 拦成 422，前端只能看到
    「Invalid input」而不是「你取消了授权」。``AuthErr.OAUTH_CANCELED`` /
    ``OAUTH_PROVIDER_ERROR`` 两个错误码本就是为此准备的（此前无人抛出）。
    """
    if error:
        if error == "access_denied":
            # 用户主动取消：400 + 专属错误码，前端可据此静默回登录页而不是报错
            raise BizError(AuthErr.OAUTH_CANCELED, error_description or None)
        raise BizError(
            AuthErr.OAUTH_PROVIDER_ERROR,
            f"{error}: {error_description}".strip(": "),
        )
    if not code or not state:
        raise BizError(AuthErr.OAUTH_PROVIDER_ERROR, "oauth callback missing code/state")
    return code, state


@router.get("/github/login")
async def github_login(db: AsyncSession = Depends(get_auth_session)) -> RedirectResponse:
    """将用户重定向到 Github OAuth 授权页面。"""
    url = await service_oauth.get_github_auth_url(db, purpose="login")
    return RedirectResponse(url=url)


@router.get("/github/callback", response_model=ApiResp[AuthTokenData])
@respond
async def github_callback(
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """处理 Github OAuth 回调。验证状态令牌，交换授权码，查找或创建用户，并返回认证令牌。

    code/state 声明为可选（并接收 provider 的 error/error_description），见 _guard_oauth_query。
    """
    code, state = _guard_oauth_query(code, state, error, error_description)
    return await service_oauth.handle_github_callback(db, code, state)


@router.post("/github/login/redirect", response_model=ApiResp[OAuthRedirectResponse])
@respond
async def github_bind_redirect(
    _cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, str]:
    """返回用于绑定的 OAuth 授权 URL（从 JS 客户端调用）。"""
    # 必须带上发起者 id：state 记录的 user_id 是回调侧唯一的归属依据，缺了它
    # bind_oauth 会以 "Bind session lost its owner" 拒绝，绑定流程恒不可用。
    url = await service_oauth.get_github_auth_url(db, purpose="bind", user_id=_cur.id)
    return {"url": url}


@router.get("/github/bind-callback", response_model=ApiResp[MessageResponse])
@respond
async def github_bind_callback(
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """处理用于账号绑定的 Github OAuth 回调。

    归属用户由 OAuth state 记录携带（``bind_github`` 内部从 state 取 user_id），
    回调无需 JWT 鉴权，故不注入 CurrentUser。用户取消授权时同样只回 error，见 _guard_oauth_query。
    """
    code, state = _guard_oauth_query(code, state, error, error_description)
    return await service_oauth.bind_github(db, code, state)
