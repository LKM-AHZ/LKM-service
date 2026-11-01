"""AUTH 内部「代验密码」端点：供 LKM-bot 面板统一到社区账号。

背景：bot 面板此前维护自己的 md5 密码，与社区账号是两套凭据。统一后面板登录把用户填的
账号密码转给 AUTH 校验，通过后由**面板自己**建会话——AUTH 不签发任何访问/刷新令牌，故本端点
不是「第二个登录入口」，只是「一次凭据判定」。

装配套路与 ``router_bot_sso`` / ``router_read`` 相同：同挂 ``/auth/internal``，共用内部 Bearer
共享令牌（未配置即 fail-closed 401，不成公网面）。monolith 经 ``auth.ROUTERS``、独立 AUTH 进程
经 ``auth.main._AUTH_ROUTERS`` 分别挂载，两处都要登记，否则拆库部署恒 404。

安全面：
- 只回 ``user_id`` / ``username`` / ``account_level``，不回邮箱、手机号等 PII；
- 失败计数、锁定、审计、封禁事件全部复用 ``verify_credentials``（与公开登录同一套），
  故这条缝不会成为绕过锁定策略的撞库通道；
- 明文密码只在本跳内网传输，不落日志、不回显。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import get_auth_session
from auth.router_read import _require_internal_token
from auth.service_auth import verify_credentials

router = APIRouter(prefix="/auth/internal", tags=["auth-internal"])


class _VerifyIn(BaseModel):
    """代验请求体。长度上界与公开登录表单一致，避免内部缝成为大包入口。"""

    account: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    ip_address: str = Field(default="", max_length=64)


class _VerifyOut(BaseModel):
    """代验结果：只透出调用方判定权限所需的最小字段。"""

    user_id: uuid.UUID
    username: str
    account_level: str


@router.post("/verify-password", response_model=_VerifyOut)
async def internal_verify_password(
    body: _VerifyIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> _VerifyOut:
    """校验一对账号密码，返回最小身份信息（不签发令牌）。

    Args:
        body: 账号 + 明文密码 + 可选客户端 IP（用于失败审计）。
        _auth: 内部共享令牌守卫（fail-closed）。
        db: 认证库会话。

    Returns:
        校验通过的用户最小视图。

    Raises:
        BizError: 401 凭据错误 / 403 准入未通过 / 423 账号锁定（由 ``verify_credentials`` 抛出）。
    """
    user = await verify_credentials(db, body.account, body.password, body.ip_address)
    return _VerifyOut(
        user_id=user.id,
        username=user.username,
        account_level=user.account_level,
    )
