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

协议常量（audience/type/issuer/ttl/account_level）是**签发侧唯一副本**，消费侧（LKM-bot 的
``astrbot/lkm/sso.py``）同名同默认值，两侧用注释互指。跨部署单元无法共享 import，故真正的
单一来源落在**部署层**：``.env``/``docker-compose.yml`` 的 ``LKM_BOT_SSO_*`` 只写一次默认值，
经 YAML 锚点同时注入 auth 与 lkmbot 两个服务（两侧读同名变量）。

本模块是 auth 包内部件；app 侧**不得**直接 import，只能经 ``auth.seams.mint_bot_sso_ticket``。
"""

from __future__ import annotations

import datetime
import os
import uuid

from auth import jwt_keys


def _protocol_value(env_name: str, default: str) -> str:
    """协议值：同名环境变量可覆盖（未配置/空白 → 代码默认值）。

    模块级求值 = 进程启动时定值（与 settings 同口径）；两侧默认值必须逐字相同，否则票据
    会被对面拒收。改这里的默认值，必须同步 LKM-bot ``astrbot/lkm/sso.py`` 的同名默认值
    与 ``.env.example`` 的说明。
    """
    return os.environ.get(env_name, "").strip() or default


#: bot 面板 SSO 专属 audience（与 lkm:admin / 前台会话隔离）。部署层暴露为
#: LKM_BOT_SSO_AUDIENCE（见 x-bot-sso-env）：这是票据的**隔离边界**，多面板部署可能要区分。
BOT_SSO_AUD = _protocol_value("LKM_BOT_SSO_AUDIENCE", "lkm:bot")
#: 票据类型，bot 侧显式校验。部署层暴露为 LKM_BOT_SSO_TYPE——签发/消费两侧是唯一配对，
#: 改它必须两侧同值，否则票据被对面拒收（type 不符即拒）。
BOT_SSO_TYPE = _protocol_value("LKM_BOT_SSO_TYPE", "bot_sso")
#: 签发方标识：bot 侧验签时校验 ``iss``（本次补齐的漏洞）。部署层暴露为 LKM_BOT_SSO_ISSUER
#: ——签发方身份随环境而变的可能性最大（多租户/多套 auth），且必须两侧同值。
BOT_SSO_ISSUER = _protocol_value("LKM_BOT_SSO_ISSUER", "lkm-auth")
#: 票据只换**管理员**面板会话，故 account_level 是协议的一部分（bot 侧同样校验）。
#: 部署层暴露为 LKM_BOT_SSO_ACCOUNT_LEVEL——但注意**这个值就是铸票门禁本身**：
#: :func:`mint_ticket` 与 ``router_bot_sso`` 都拿它做相等比较，调低它等于把门禁降级
#: （设成普通用户等级，普通用户即可持票换管理员面板会话）。两侧必须同值。
BOT_SSO_ACCOUNT_LEVEL = _protocol_value("LKM_BOT_SSO_ACCOUNT_LEVEL", "admin")


#: TTL 上界（秒）。票据在 iframe URL query 里明文传递，会进浏览器历史/代理日志，
#: 故上界的意义是「即使配错也不会长期可重放」——60s 默认够一次重定向往返，5 分钟已很宽裕。
_TTL_MAX_SECONDS = 300


def _ttl_seconds(env_name: str, default: int) -> int:
    """TTL 秒数：环境变量可覆盖，非法值回落默认，超上界则钳到上界。

    下界同样钳制（非正值/非数字 → 默认）：宁可 60s，也不让签发出一个荒谬的有效期；
    上界钳制见 :data:`_TTL_MAX_SECONDS`。
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return min(value, _TTL_MAX_SECONDS)


#: 票据有效期（秒）。票据经 iframe URL query 传递，必须短到「来不及被日志/历史二次利用」，
#: 又要容得下浏览器一次重定向的往返。部署层暴露为 LKM_BOT_SSO_TTL_SECONDS——仅签发侧消费
#: （消费侧由 PyJWT 按 ``exp`` 自行判定），故调整不影响对面；配大了也会被上界钳住。
BOT_SSO_TTL_SECONDS = _ttl_seconds("LKM_BOT_SSO_TTL_SECONDS", 60)


def mint_ticket(*, sub: str, account_level: str) -> tuple[str, int]:
    """铸一张 bot 面板 SSO 票据，返回 ``(ticket, expires_in_seconds)``。

    ``account_level`` 不是管理员（:data:`BOT_SSO_ACCOUNT_LEVEL`）时直接抛 ``ValueError``
    （fail-closed）：本票据唯一用途是给 bot 面板换管理员会话，非管理员不该拿到票；调用方
    （internal 端点）另有一层 403。
    """
    if str(account_level) != BOT_SSO_ACCOUNT_LEVEL:
        raise ValueError(
            f"bot SSO ticket requires account_level={BOT_SSO_ACCOUNT_LEVEL}"
        )
    subject = str(sub).strip()
    if not subject:
        raise ValueError("bot SSO ticket requires a non-empty subject")

    now = datetime.datetime.now(datetime.UTC)
    payload: dict[str, object] = {
        "iss": BOT_SSO_ISSUER,
        "aud": BOT_SSO_AUD,
        "sub": subject,
        "account_level": BOT_SSO_ACCOUNT_LEVEL,
        "type": BOT_SSO_TYPE,
        # 一次性消费标识：bot 侧落 TTL 表，重复出现即拒（防重放）。
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(seconds=BOT_SSO_TTL_SECONDS)).timestamp()),
    }
    return jwt_keys.encode(payload), BOT_SSO_TTL_SECONDS
