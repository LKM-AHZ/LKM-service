"""后台 bot 面板集成端点（LKM-bot 面板并入社区后台）。

现状：bot 面板以社区站同域子路径 ``/bot/`` 暴露（网关 ``proxy-rewrite`` 剥前缀后送 lkmbot），
后台每个「机器人」页面用同源 iframe 内嵌它。管理员已登录社区后台即不该二次登录，故本端点提供
**票据换取**的第一步：代表当前 admin 会话铸一张 60s 一次性票据，由后台 SSR 页拼进 iframe URL。

职责边界：**只鉴权与转发，不签发**。后台进程是「只验签方」（不持签发私钥）——票据签发原语在
auth 域，经 ``auth.seams.mint_bot_sso_ticket`` 走内部 HTTP 缝取。缝不可用即抛
``BizError(UNAVAILABLE)``（503），绝不返回空票让前端以为「已免登」而静默降级。

安全面：身份由 ``require_admin``（seam-only，auth 权威裁决）确定；票据只换 bot 面板自身会话，
不是社区侧授权凭据（``aud=lkm:bot``，见 ``auth/bot_sso``）。
"""

from fastapi import APIRouter

from app.core.common import ApiResp
from app.core.err import respond
from auth.deps import CurrentUser
from auth.seams import mint_bot_sso_ticket

from .deps import require_admin
from .schemas import AdminBotSsoTicket

router = APIRouter(prefix="/admin/bot", tags=["admin-bot"])


@router.post("/sso-ticket", response_model=ApiResp[AdminBotSsoTicket])
@respond
async def admin_bot_sso_ticket(
    cur: CurrentUser = require_admin,
) -> AdminBotSsoTicket:
    """为当前后台管理员铸一张 bot 面板 SSO 票据（一次性、60s）。

    后台 SSR 页在渲染 iframe 前调用；票据经 iframe URL 传给 bot 面板的
    ``/api/v1/auth/sso`` 端点，由 bot 验签后自建面板会话。
    """
    issued = await mint_bot_sso_ticket(cur.id, cur.account_level)
    return AdminBotSsoTicket(
        ticket=str(issued["ticket"]),
        expires_in=int(issued["expires_in"]),
    )
