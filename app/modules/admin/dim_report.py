"""离线报表宽表 ``user_dim`` 的只读 read port（M3.B0.3，读侧）。

**OFFLINE-ONLY 读取边界（本次收束落笔）**：本模块是对 ``app.db.user_dim.UserDim``（B0.1
离线反范式宽表，B0.2 由 auth 源 ETL/事件已填充）的**唯一 intended 报表读口**。它只读
``user_dim``，**绝不经在线热路径读源（users/profiles/user:snap/auth 缝）**，也**绝不相耦任何
管理/改动作**：
- 在线身份/管理与动作读（admin 对某用户 ban/edit/权限/头像等 read-then-write）走 A4
  ``auth.snapshot.list_user_snapshots`` 实时缝 + ``user:snap`` 缓存——**保持原样，零改动**。
- admin 实时仪表盘 ``/admin/stats``、``/admin/stats/trend`` 仍是**在线实时**计数（读在线
  users/content_items/library_file，反映同请求内新建，不容忍 dim 滞后）；**不得**把这类
  实时仪表盘切去读 dim（会把"实时 dashboard"误标成离线 report，或引入陈旧仪表盘）。本任务
  scope 只交付「报表读侧已与在线读隔离」的**数据面 + port**：dim 已被 B0.2 填充新鲜，而
  没有任何实时/在线 admin 动作改读 dim。

据此，本树 admin **没有**任何独立"纯离线 user REPORT/总览/export 消费面"（现有用户读：
``/admin/users``＝A4 管理/动作列表、``/admin/stats(/trend)``＝实时仪表盘、/admin/reports＝
内容举报）。故 B0.3 **不发明**新的报表路由/UI，也不把这些实时读误标为报告去 repoint——只
交付可被未来 B1/域路线图的离线报表消费方现成接用的只读 port + 边界 seg 测 + 本注释作账。

落位选择：放 ``app/modules/admin/``（业务模块），import 的是 ``app.db.user_dim``（基础设施
db 层，非业务模块）——modules→db 方向，是既有 admin routers 也有的同方向边（如
``app.db.session``），**零新增 import-linter 违约边**（契约二拦的是 db→modules 反向；
契约三/四只拦 business→business / business→auth 内部，``app.db.user_dim`` 均不在
其 forbidden 集内）。宽表语义归属 auth＝单一数据源 owner（写侧在 auth/user_dim_sync），此处
只是 read-only 离线的**读方**，不构成任何写/管理参与方。

读接口与 A4 管理列表同构但独立：返回 ``(id desc 一页 DimUserRow, 过滤后 total)``，路由/消费
方据此组 ``PageData``。PII(镜像 email)沿用 ``include_pii`` 门控——默认在构造响应时置 None、
不落进报表，杜绝离线副本 PII 经报表横向散布。本模块不带鉴权——授权由接入方（路由/进程）按
其现有门槛负责。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.user_dim import UserDim

from .schemas import DimUserRow

# 单次查询行数上限：本读口是无鉴权内部 port，正常消费方分页读，不需要一次拉全表
_MAX_LIMIT = 500

# LIKE 转义（与 app/modules/search/repository.py 同口径）。不能直接 import 那边的私有
# 助手：import-linter 合同禁止业务模块间任意依赖，dim_report→search.repository 未在豁免列。
_LIKE_ESCAPE = "\\"


def _like_pattern(term: str) -> str:
    """把用户输入转成 ILIKE 模式串，转义 ``%``/``_``/反斜杠，避免通配符全表匹配。"""
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


async def list_user_dim(
    db: AsyncSession,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    include_pii: bool = False,
) -> tuple[list[DimUserRow], int]:
    """离线报表宽表分页读（B0.3 read port）。返回 ``(id desc 一页行, 过滤后 total)``。

    - 只读 ``user_dim``（离线副本），不作任何管理/写/动作；mirror A4 的 offset+total
      分页形态以便消费方对接 ``PageData``。
    - ``q`` 关键字仅按 ``UserDim.username`` 模糊匹配（与 A4 一致，不做泄露式按邮箱枚举）。
    - ``include_pii=False``（默认）：镜像 email 在构造响应时一律置 None，绝不落进报表
      响应；仅 gate 开启才让它随非 PII accounting 列一起带出（离线副本 PII 亦按需最小化，
      杜绝经报表横向散布）。
    - 本函数不带鉴权；授权由接入方按其门槛负责（对标管理面 A4 读口同处置）。
    - sync_ts 供读方判各行的物化新鲜度；若需"绝不读出未对账过的陈旧行"由接入方过滤。
    - ``offset``/``limit`` 在此 clamp：负数在 PG 上直接 `ProgrammingError: LIMIT must not be
      negative`（500 而非空页），超大 limit 则会在单请求里把整张宽表拉出来。
    """
    offset = max(0, offset)
    limit = max(1, min(limit, _MAX_LIMIT))
    count_q = select(func.count(UserDim.user_id))
    cond = (
        UserDim.username.ilike(_like_pattern(q), escape=_LIKE_ESCAPE) if q else None
    )
    if cond is not None:
        count_q = count_q.where(cond)
    total = int((await db.execute(count_q)).scalar_one() or 0)

    stmt = select(UserDim)
    if cond is not None:
        stmt = stmt.where(cond)
    rows = (
        await db.execute(stmt.order_by(UserDim.user_id.desc()).offset(offset).limit(limit))
    ).scalars()

    # PII(email) 仅在 gate 开启时才落进响应；否则一律 None（离线副本 PII 不外泄/横向散布）
    items = [
        DimUserRow(
            user_id=d.user_id,
            username=d.username,
            account_level=d.account_level,
            is_banned=d.is_banned,
            is_locked=d.is_locked,
            created_at=d.created_at,
            sync_ts=d.sync_ts,
            nickname=d.nickname,
            role=d.role,
            email=d.email if include_pii else None,
        )
        for d in rows
    ]
    return items, total
