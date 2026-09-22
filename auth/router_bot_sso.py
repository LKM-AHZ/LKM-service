"""AUTH 内部 bot-SSO 铸票端点：供业务进程（后台）为代表已裁决的管理员换取一次性票据。

装配：与 ``router_read`` / ``router_authz`` 同挂 ``/auth/internal``，共用同一内部 Bearer 共享令牌
（未配置即 fail-closed 401，不成公网面）。monolith（经 registry ROUTERS）与独立 AUTH 进程
（auth.main 显式挂）都挂载它。

职责边界：本端点**只铸票不建会话**——票据由 bot 面板自行验签并换其面板会话（见
``auth.bot_sso`` 模块 docstring、社区侧 ``app/modules/admin/bot_router``）。故这里不需要 DB 会话，
也不透出任何 PII：请求体只有管理员 id 与 account_level，响应体只有票据与有效期。

安全面：调用方必须已经是**经 authz seam 裁决过的 admin**（业务侧 ``require_admin``），本端点再做
一次 ``account_level == BOT_SSO_ACCOUNT_LEVEL`` 复核——跨进程边界不信任上游断言（fail-closed）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.bot_sso import BOT_SSO_ACCOUNT_LEVEL, mint_ticket
from auth.router_read import _require_internal_token

router = APIRouter(prefix="/auth/internal", tags=["auth-internal"])


class _BotTicketIn(BaseModel):
    user_id: uuid.UUID
    account_level: str


@router.post("/bot-ticket")
async def internal_bot_ticket(
    body: _BotTicketIn,
    _auth: None = Depends(_require_internal_token),
) -> dict[str, object]:
    """铸一张 bot 面板 SSO 票据。返回内部信封 ``{"ticket": str, "expires_in": int}``。

    非 :data:`BOT_SSO_ACCOUNT_LEVEL` → 403（跨进程边界不信任上游已裁决的断言，此处独立复核）。
    403 文案从该常量派生，避免它被部署配置改掉后报错信息仍写着 admin。
    """
    if body.account_level != BOT_SSO_ACCOUNT_LEVEL:
        raise HTTPException(
            status_code=403, detail=f"{BOT_SSO_ACCOUNT_LEVEL} required"
        )
    ticket, expires_in = mint_ticket(
        sub=str(body.user_id), account_level=body.account_level
    )
    return {"ticket": ticket, "expires_in": expires_in}
