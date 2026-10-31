"""后台 bot 面板集成端点（LKM-bot 面板并入社区后台）。

现状：bot 面板以社区站同域子路径 ``/bot/`` 暴露（网关 ``proxy-rewrite`` 剥前缀后送 lkmbot），
后台每个「机器人」页面用同源 iframe 内嵌它。管理员已登录社区后台即不该二次登录，故本端点提供
**票据换取**的第一步：代表当前 admin 会话铸一张 60s 一次性票据，由后台 SSR 页拼进 iframe URL。

职责边界：**只鉴权与转发，不签发**。后台进程是「只验签方」（不持签发私钥）——票据签发原语在
auth 域，经 ``auth.seams.mint_bot_sso_ticket`` 走内部 HTTP 缝取。缝不可用即抛
``BizError(UNAVAILABLE)``（503），绝不返回空票让前端以为「已免登」而静默降级。

安全面（评审阻断项 R3 之后的口径）：

- 身份由 ``require_admin``（seam-only，auth 权威裁决）确定；
- **授权另有独立能力点** ``admin.bot_panel``，不是「account_level == admin」的同义词。
  票据换来的 bot 面板会话等于 Bot 控制面（插件、宿主编排），所以社区 admin 会话被盗不该
  自动升级成面板全权：撤销这一条授权即可单独断链，不必停用账号（停用会连带内容管理一起失效）。
- 成功与拒绝都写审计（``audit_logs`` 属 auth 域，经 ``record_audit`` 跨 realm）：拒绝也要留痕，
  「能力点被撤销后仍在换票」是会话可能被盗的早期信号。
- 成功路径**审计 fail-closed**：审计写不进去就不交票（票据已铸但从未下发，60s 自行过期）。
- 仍未做（跨仓）：票据绑定来源 IP / 会话指纹需要 auth 签发侧与 bot 面板消费侧同时改并各自
  校验，属独立变更；本端点不臆造这一约束。

票据只换 bot 面板自身会话，不是社区侧授权凭据（``aud=lkm:bot``，见 ``auth/bot_sso``）。
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rbac.permissions import Permission
from core.client_ip import client_ip
from core.common import ApiResp
from core.contracts import CurrentUser
from core.db.session import get_session
from core.err import BizError, CommonErr, respond
from core.ports.audit import record_audit
from core.ports.users import mint_bot_sso_ticket

from .deps import require_admin
from .permissions import require_permission
from .schemas import AdminBotSsoTicket

router = APIRouter(prefix="/admin/bot", tags=["admin-bot"])

# 审计动作名：前缀按域分组，便于按 action 前缀筛「面板换票」全量事件。
_ACTION_ISSUED = "admin_bot_panel_ticket_issued"
_ACTION_DENIED = "admin_bot_panel_access_denied"


@router.post("/sso-ticket", response_model=ApiResp[AdminBotSsoTicket])
@respond
async def admin_bot_sso_ticket(
    request: Request,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotSsoTicket:
    """为持有 ``admin.bot_panel`` 的后台管理员铸一张 bot 面板 SSO 票据（一次性、60s）。

    后台 SSR 页在渲染 iframe 前调用；票据经 iframe URL 传给 bot 面板的
    ``/api/v1/auth/sso`` 端点，由 bot 验签后自建面板会话。

    无该能力点 → 403（``Missing permission: admin.bot_panel``，前端据此隐藏入口并提示），
    拒绝事件写审计；有该能力点但签发缝不可用 → 503，绝不返回空票。
    """
    try:
        await require_permission(db, cur, Permission.admin_bot_panel)
    except BizError:
        # 拒绝**不得**因为审计写失败而变成 503/500：那会把越权探针伪装成可用性故障。
        # 审计写失败时先回滚会话——否则一个处于 aborted 状态的事务会在依赖收尾的 commit
        # 处再炸一次，403 就变成了 500。
        try:
            await record_audit(
                db,
                cur.id,
                _ACTION_DENIED,
                detail=(
                    f"role={cur.role} account_level={cur.account_level}"
                    " reason=missing admin.bot_panel"
                ),
                ip_address=client_ip(request),
            )
        except Exception:
            await db.rollback()
        raise

    issued = await mint_bot_sso_ticket(cur.id, cur.account_level)
    ticket = issued.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise BizError(CommonErr.UNAVAILABLE, "Bot SSO ticket malformed")
    try:
        expires_in = int(issued["expires_in"])
    except (KeyError, TypeError, ValueError):
        raise BizError(CommonErr.UNAVAILABLE, "Bot SSO ticket malformed") from None

    # fail-closed：审计写不进去就不交票。票据已经铸出但从未下发，60s 后自行过期。
    await record_audit(
        db,
        cur.id,
        _ACTION_ISSUED,
        detail=f"expires_in={expires_in} aud=lkm:bot",
        ip_address=client_ip(request),
    )
    return AdminBotSsoTicket(ticket=ticket, expires_in=expires_in)
