"""
后台只读数据端点：/admin/users（用户列表）、/admin/stats（仪表盘统计）。

S5-A2 Step2 拆库后：user 真值只在 **auth 库 lkm_auth**（users/profiles 已迁出 monolith
biz Base）；ROLE 权限栅 RolePermission 仍在 **biz 库**。故每个数据面端点按情况持**两会话**：
- ``db``（biz ``get_read_session``）：仅作者 role/权限判定 + content/files 聚合。
- ``auth``（auth ``get_auth_session`` 産出的**只读**序列声明）：用户列表/总数/趋势一律经
  ``auth.snapshot`` 读缝从 **auth authoritative** 取——本文件**不再本地跨库
  select/count/auth User(biz db)**（users 不在 biz realm）。
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rbac.permissions import Permission
from core.common import (
    ApiResp,
    ListData,
    PageData,
    PaginateDep,
    PaginateParams,
    paginate_pages,
)
from core.contracts import CurrentUser
from core.db.session import get_read_session
from core.err import respond
from core.ports import content as content_port
from core.ports import files as files_port
from core.ports.audit import new_auth_session as _new_auth_session_raw
from core.ports.snapshot import (
    count_active_users,
    list_user_snapshots,
    user_count_by_day,
)

from .deps import require_admin
from .permissions import require_permission
from .schemas import AdminStats, AdminTrendItem, AdminUserListItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-data"])


async def get_admin_auth_read_session() -> AsyncIterator[AsyncSession]:
    """admin 数据面读 auth authoritative 用的 **auth 库只读会话**（yield → FastAPI 于请求末负责关）。

    拆库后 user 真值只在 auth 库。数据面 reader 需同时问 biz(role/聚合) 与 auth(users 列表/
    数/趋势)，故给 reader 端点再加一个 auth 会话；本函数把 ``auth.seams.new_auth_session``
    包成 **yield-generator dependency** —— 生产时 new 一个真实 auth 会话并在此收尾 close；测试/多
    进程拆分前同源码单进程两会话分连两库也合法。消费方只可做**只读**（读 auth.snapshot 数字/
    列表缝），绝不做写。授权(RBAC RolePermission)仍在 biz，不在本会话判。

    取会话必须用 ``new_auth_session``（普通协程工厂）而非 ``get_auth_session``：后者是
    async generator 函数，``await`` 其返回值会抛 TypeError（本依赖每次请求都 500，
    测试因 dependency_overrides 覆盖了本依赖而掩盖）。
    """
    sess = await _new_auth_session_raw()
    try:
        yield sess
    finally:
        await sess.close()


@router.get("/users", response_model=ApiResp[PageData[AdminUserListItem]])
@respond
async def admin_list_users(
    keyword: str | None = None,
    include_pii: bool = False,
    _cur: CurrentUser = require_admin,
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
    auth: AsyncSession = Depends(get_admin_auth_read_session),
) -> PageData[AdminUserListItem]:
    """
    用户管理列表。默认隐藏 email/phone（PII），`keyword` 仅按**用户名**筛选
    (邮箱不展示也不参与筛选，避免泄露式枚举)。
    授权门槛(require_admin 之上 + require_permission)在本路线判定（biz db 的
    RolePermission），随后把 include_pii 布尔透传给 auth 读缝 ``list_user_snapshots``
    ——用户列表真值走 **auth 库会话**(auth) 的 auth authoritative（users 不在 biz realm，
    绝不用 biz db 跨库读 User）。分页/门控语义收敛到缝，响应 JSON 形态保持不变。
    """
    await require_permission(db, _cur, Permission.admin_users_manage)
    # 行查询 + 过滤后 total 均由缝(list_user_snapshots, auth 会话)产出：id desc + offset 分页
    rows, total = await list_user_snapshots(
        auth, q=keyword, offset=pag.offset, limit=pag.limit, include_pii=include_pii
    )
    # 返回 schema 实例而非 model_dump，使响应体为 PageData 实例（Task 1 依赖
    # isinstance 判定位以自动附带 X-Total 头）。
    items = [AdminUserListItem.model_validate(m, from_attributes=True) for m in rows]

    return PageData(
        items=items,
        total=total,
        page=pag.page,
        pages=paginate_pages(total, pag.limit),
    )


async def _safe_call(fn: Any, *args: Any) -> int:
    """单个聚合调用容错：某模块读取失败时不拖垮整页统计（对聚合类后台端点友好）。

    必须记日志并 rollback：静默 return 0 让真实故障与「本来就是 0」不可区分；而 PG 事务
    一旦出错即进入 aborted 状态，不 rollback 会让本请求后续所有计数器也连带归零。
    """
    try:
        return int(await fn(*args))
    except Exception:
        logger.exception("admin stats counter failed; reporting 0")
        with contextlib.suppress(Exception):
            await args[0].rollback()
        return 0


@router.get("/stats", response_model=ApiResp[AdminStats])
@respond
async def admin_stats(
    _cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_read_session),
    auth: AsyncSession = Depends(get_admin_auth_read_session),
) -> AdminStats:
    """仪表盘聚合统计：注册用户数 / 帖子数 / 文件数 / 待审核文件数。

    - 用户总数是真值只在 auth 库——经 ``auth.snapshot.count_active_users(auth)`` 走 auth
      authoritative，**不再以 biz db 跨库 count User**。
    - RBAC(role) 判定仍在 biz ``db``；帖子/文件聚合(biz 表)仍在 ``db``。
    """
    await require_permission(db, _cur, Permission.admin_dashboard)
    user_count = await count_active_users(auth)
    post_count = await _safe_call(content_port.count_discussions, db)
    file_count = await _safe_call(files_port.count_files, db)
    file_pending = await _safe_call(files_port.count_files, db, "pending")
    return AdminStats(
        user_count=user_count,
        post_count=post_count,
        file_count=file_count,
        file_pending_count=file_pending,
    )


@router.get("/stats/trend", response_model=ApiResp[ListData[AdminTrendItem]])
@respond
async def admin_trend(
    days: int = Query(14, ge=1, le=90),
    _cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_read_session),
    auth: AsyncSession = Depends(get_admin_auth_read_session),
) -> ListData[AdminTrendItem]:
    """后台趋势：最近 days 天每日新增注册用户数 + 新增帖子数（日期连续、缺日补 0）。

    S5-A2 Step2 拆分数据源：
    - **user 增量**真值只在 auth 库 → 经 ``auth.snapshot.user_count_by_day(auth, ...)`` 走
      **auth authoritative**（不再以 biz db 跨库 ``func.date(User.created_at)``）。
    - **帖子增量**(content_items) 仍查 biz ``db``，但经 content 只读端口（口径归 content）。
    - RBAC role 判定仍在 biz ``db``。
    """

    await require_permission(db, _cur, Permission.admin_dashboard)

    start = datetime.now(UTC).date() - timedelta(days=days - 1)
    # user 增量：auth authoritative（auth.snapshot 的 UTC 分桶，.where created_at >= start
    # 且 < start+days 只覆盖窗口；窗口内缺日由下方循环补 0）
    user_d = await user_count_by_day(auth, start=start, days=days)
    # 帖子统计改走统一写源 content_items（content_type == discussion ⇔ 原 forum_posts）biz
    post_d = await content_port.daily_discussion_counts(db, start, days)

    items: list[AdminTrendItem] = []
    for i in range(days):
        d = start + timedelta(days=i)
        items.append(
            AdminTrendItem(
                date=d,
                user_delta=int(user_d.get(d, 0)),
                post_delta=int(post_d.get(d, 0)),
            )
        )
    return ListData(items=items)
