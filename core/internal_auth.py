"""业务侧内部服务令牌守卫：bot → backend 的服务身份。

背景：LKM-bot 需要把产出的媒体（TTS 音频、T2I 图片、聊天附件）写进后端的对象存储，并读回
社区文件库的内容。这类调用**没有社区用户身份**——调用方是另一个服务，不是浏览器会话，因此
不能复用 ``get_current_user`` 那一套。

为什么另起一把密钥而不复用 ``auth_http_token``：那把只覆盖 ``/auth/internal/*``（认证域读面）。
让存储面共用它，等于一处泄漏同时打穿「认证内部读面」与「可写对象存储」，两个域的爆炸半径被
合并。故此处使用独立令牌 ``LKM_BOT_INTERNAL_TOKEN``，由部署层同时注入 backend 与 lkmbot。

与 ``auth.router_read._require_internal_token`` 同构的三条语义：
1. 未配置令牌 → 401（fail-closed）：忘配时该面**关闭**，而不是退化成公开可写存储桶；
2. 缺失/非 Bearer/不匹配 → 401，错误信息不回显令牌；
3. 比字节而非 str：``Authorization`` 头由 ASGI 服务器按 latin-1 解码，攻击者带 >=0x80 的字节
   会让 str 版 ``compare_digest`` 抛 TypeError（500 + 栈），把这唯一门禁变成报错面。
"""

from __future__ import annotations

import secrets

from fastapi import Header

from core.config import settings
from core.err import BizError, CommonErr
from core.secrets import reveal


def bot_service_configured() -> bool:
    """bot 服务身份是否已配置（供状态/健康自检展示，不做鉴权判定）。"""
    return bool(reveal(settings.bot_internal_token).strip())


def _deny(reason: str) -> None:
    """统一以 ``BizError`` 拒绝：所有端点走同一全局 handler 收敛到统一信封。"""
    raise BizError(CommonErr.UNAUTHORIZED, reason)


async def require_bot_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """依赖：校验 ``Authorization: Bearer <LKM_BOT_INTERNAL_TOKEN>``。

    Args:
        authorization: 原始 Authorization 头，缺省为 None。

    Raises:
        BizError: 401——未配置令牌、缺头、非 Bearer、或令牌不匹配。
    """
    token = reveal(settings.bot_internal_token).strip()
    if not token:
        _deny("internal bot service is not configured")
    if not authorization:
        _deny("missing Authorization")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        value.encode("utf-8"), token.encode("utf-8")
    ):
        _deny("bad internal bot token")
