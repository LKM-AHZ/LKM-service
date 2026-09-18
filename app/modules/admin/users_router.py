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
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import (
    ApiResp,
    ListData,
    PageData,
    PaginateDep,
    PaginateParams,
    paginate_pages,
)
from app.core.err import respond
from app.db.auth_session import get_auth_session as _get_auth_session_raw
from app.db.session import get_read_session
from app.modules.auth.deps import CurrentUser
from app.modules.auth.snapshot import (
    count_active_users,
    list_user_snapshots,
    user_count_by_day,
)
from app.modules.content.models import ContentItem, ContentType
from app.modules.files.models import LibraryFile
from app.modules.rbac.permissions import Permission

from .deps import require_admin
from .permissions import require_permission
from .schemas import AdminStats, AdminTrendItem, AdminUserListItem

router = APIRouter(prefix="/admin", tags=["admin-data"])


async def get_admin_auth_read_session() -> AsyncIterator[AsyncSession]:
    """admin 数据面读 auth authoritative 用的 **auth 库只读会话**（yield → FastAPI 于请求末负责关）。

    拆库后 user 真值只在 auth 库。数据面 reader 需同时问 biz(role/聚合) 与 auth(users 列表/
    数/趋势)，故给 reader 端点再加一个 auth 会话；本函数把 ``app.db.auth_session.get_auth_session``
    包成 **yield-generator dependency** —— 生产时 new 一个真实 auth 会话并在此收尾 close；测试/多
    进程拆分前同源码单进程两会话分连两库也合法。消费方只可做**只读**（读 auth.snapshot 数字/
    列表缝），绝不做写。授权(RBAC RolePermission)仍在 biz，不在本会话判。
    """
    sess = await _get_auth_session_raw()
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
    items = [
        AdminUserListItem(
            id=m.id,
            username=m.username,
            account_level=m.account_level,
            is_locked=m.is_locked,
            created_at=m.created_at,
            email=m.email,
            phone=m.phone,
        )
        for m in rows
    ]

    return PageData(
        items=items,
        total=total,
        page=pag.page,
        pages=paginate_pages(total, pag.limit),
    )


async def _safe_count(db: AsyncSession, stmt: Any) -> int:
    """单计数器容错：某模块表缺失/不可用时不拖垮整页统计（对聚合类后台端点友好）。"""
    try:
        return (await db.execute(stmt)).scalar() or 0
    except Exception:
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
    post_count = await _safe_count(
        db,
        select(func.count(ContentItem.id)).where(
            ContentItem.content_type == ContentType.DISCUSSION
        ),
    )
    file_count = await _safe_count(db, select(func.count(LibraryFile.id)))
    file_pending = await _safe_count(
        db,
        select(func.count(LibraryFile.id)).where(LibraryFile.status == "pending"),
    )
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
    - **帖子增量**(content_items)仍在 biz ``db``；``_biz_deltas`` 只对 biz 表执行。
    - RBAC role 判定仍在 biz ``db``。
    """

    await require_permission(db, _cur, Permission.admin_dashboard)

    async def _biz_deltas(col: Any, extra_where: Any | None = None) -> dict[date, int]:
        """按某 biz 时间列分组统计每日增量；单表异常返回空 dict（_safe_count 同款容错）。
        extra_where 可选附加过滤（如 content_type == discussion）。
        func.date 返回日期文本，统一转 date 作 key。本处的语义约束：
        消费方是 biz 表（posts 等仍在 biz realm），不做跨库假设。"""
        out: dict[date, int] = {}
        try:
            # 统一按 UTC 分桶：PG 的 func.date(timestamptz) 会先按会话时区(本地+08)取日，
            # 与“以 UTC 今天为基准”偏移一天；故先 AT TIME ZONE 'UTC' 变 naive-UTC 再取日。
            day_expr = func.date(func.timezone("UTC", col))
            # 右界同理须用 UTC aware datetime：date 参数会被 PG 按会话时区解释而整体
            # 偏移（见 auth.snapshot.user_count_by_day 同款说明）。
            start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
            stmt = select(day_expr.label("d"), func.count()).where(col >= start_dt)
            if extra_where is not None:
                stmt = stmt.where(extra_where)
            rows = (await db.execute(stmt.group_by("d"))).all()
        except Exception:
            return out
        for r in rows:
            raw = r[0]
            if raw is None:
                continue
            r0: str = raw if isinstance(raw, str) else str(raw)
            with contextlib.suppress(ValueError):
                out[date.fromisoformat(r0[:10])] = int(r[1] or 0)
        return out

    # 数据按 UTC 存储/分桶，基准须用 UTC 的"今天"，否则本地时区偏移（东8区凌晨）
    # 会让 start 与分桶错位一天（跨天不 flaky，同 admin_trend 既有语义）。
    start = datetime.now(UTC).date() - timedelta(days=days - 1)
    # user 增量：auth authoritative（auth.snapshot 的 UTC 分桶，.where created_at >= start
    # 且 < start+days 只覆盖窗口；窗口内缺日由下方循环补 0）
    user_d = await user_count_by_day(auth, start=start, days=days)
    # 帖子统计改走统一写源 content_items（content_type == discussion ⇔ 原 forum_posts）biz
    post_d = await _biz_deltas(
        ContentItem.created_at,
        extra_where=ContentItem.content_type == ContentType.DISCUSSION,
    )

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
