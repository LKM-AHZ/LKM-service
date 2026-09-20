"""BOT 面板 SSO 票据签发原语（LKM-bot 面板并入社区后台）。

背景：LKM-bot 面板由独立子域 ``bot.lkm-ahz.ltd`` 改为社区站同域子路径 ``/bot/`` 后，管理员
已登录社区后台（``admin_session`` cookie）再打开 iframe 面板时不应二次登录。本模块提供**一次性
短期票据**的签发原语：社区侧凭有效 admin 会话铸票 → 浏览器带票访问 bot 面板的 SSO 端点 →
bot 用**公钥**验签后自建面板会话。

密钥路线：**复用批 5 的 RS256**（``auth.jwt_keys``）——私钥只在 auth 进程，bot 侧只持公钥
（挂载与网关同一份 ``deploy/jwt/keys``）。不引入第二套密钥体系；对称共享密钥会让 bot 也能伪造
社区票据，扩大爆炸半径。未配私钥时 ``jwt_keys.encode`` 回落 HS256（本地开发/测试），此时 bot
侧无 RS256 公钥 → 验签失败 → 回落 bot 自带登录页（fail-safe，不退化为越权）。

票据语义（**只用于换 bot 面板会话，不作任何社区侧授权凭据**）：
- ``aud=lkm:bot``：与 ``lkm:admin`` 前台/后台会话隔离，票据被其它面拒收；
- ``type=bot_sso``：bot 侧显式校验，防拿别的 token 冒充；
- ``account_level=admin``：仅管理员可铸（:func:`mint_ticket` 内 fail-closed 断言）；
- ``jti`` + 60s TTL：bot 侧一次性消费（防重放）；票据经 iframe URL query 传递，故 TTL 必须短；
- 不编 ``token_version``/``mfa``：bot 无法查社区 auth 库做版本复核，票据本身已短期且一次性，
  编入反而给出「已复核」的假象。

本模块是 auth 包内部件；app 侧**不得**直接 import，只能经 ``auth.seams.mint_bot_sso_ticket``。
"""

from __future__ import annotations

import datetime
import uuid

from auth import jwt_keys

#: bot 面板 SSO 专属 audience（与 lkm:admin / 前台会话隔离）。
BOT_SSO_AUD = "lkm:bot"
#: 票据类型，bot 侧显式校验。
BOT_SSO_TYPE = "bot_sso"
#: 签发方标识。
BOT_SSO_ISSUER = "lkm-auth"
#: 票据有效期（秒）。票据经 iframe URL query 传递，必须短到「来不及被日志/历史二次利用」，
#: 又要容得下浏览器一次重定向的往返。
BOT_SSO_TTL_SECONDS = 60


def mint_ticket(*, sub: str, account_level: str) -> tuple[str, int]:
    """铸一张 bot 面板 SSO 票据，返回 ``(ticket, expires_in_seconds)``。

    ``account_level`` 不是 ``admin`` 时直接抛 ``ValueError``（fail-closed）：本票据唯一用途是
    给 bot 面板换管理员会话，非管理员不该拿到票；调用方（internal 端点）另有一层 403。
    """
    if str(account_level) != "admin":
        raise ValueError("bot SSO ticket requires account_level=admin")
    subject = str(sub).strip()
    if not subject:
        raise ValueError("bot SSO ticket requires a non-empty subject")

    now = datetime.datetime.now(datetime.UTC)
    payload: dict[str, object] = {
        "iss": BOT_SSO_ISSUER,
        "aud": BOT_SSO_AUD,
        "sub": subject,
        "account_level": "admin",
        "type": BOT_SSO_TYPE,
        # 一次性消费标识：bot 侧落 TTL 表，重复出现即拒（防重放）。
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(seconds=BOT_SSO_TTL_SECONDS)).timestamp()),
    }
    return jwt_keys.encode(payload), BOT_SSO_TTL_SECONDS
