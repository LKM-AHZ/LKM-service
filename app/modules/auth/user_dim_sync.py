"""user_dim 离线宽表的 ETL 同步（M3.B0.2，填充腿）。

把 auth 源（``users`` + ``profiles`` 的登录锚字段）反范式物化进只读离线宽表
``app.db.user_dim.UserDim``（B0.1 建表）。**OFFLINE-ONLY**：本模块只**写**
``user_dim``，绝不经在线热路径写/读源（source 永远不可在此被改，也绝不作在线读源）。
唯一写者是 B0.3/report（后续任务）读的这张报表副本的填充侧。

**跨 realm 双会话（S5 真拆库后）**：源 ``User``/``Profile`` 在 auth 独立库，目标
``UserDim`` 在业务库，二者不在同一 PG → 本模块的每个入口都接收**一对会话**
``(source_db, target_db)``：源读一律走 ``source_db``（auth），目标写一律走 ``target_db``
（业务）。绝不在单会话里跨库 join（拆库前融合态下曾可行，拆库后必 UndefinedTable）。

为什么放在 ``app/modules/auth/``（数据源 owner 侧）而非 ``app/db/`` 或 ``app/core/``：
- 本 ETL 必须读 ``User``/``Profile``（源属 auth）。放 ``app/db`` 会被 import-linter 契约二
  （db 层不反向依赖业务模块）拦下；放 ``app/core`` 触发契约一（core 不依赖业务模块）。
  放 auth 自身（auth 读自己的源 + 写 db 层离线副本）恰好都合法：modules→db、auth→auth
  均无障碍（与 blog/files 的 ``<module>/tasks.py`` 自开会话的落位同范式）。B0.1 把*表*
  放 db/ 是为零新增 db→业务边；*ETL*（有意依赖 auth 源）落 auth，职责分归源 owner。

批量/命令计数硬性质（M3.3 roadmap）：**纯批式、绝无 per-row N+1**。每个操作 DB 命令数
与受影响用户数 ``N`` **无关（常数）**：
- ``sync_dim_for_ids``：1 条批量源读（``User`` LEFT JOIN ``Profile``，``WHERE id IN``，
  走 ``source_db``）+ 1 条批量 ``INSERT ... ON CONFLICT(user_id) DO UPDATE``（Postgres 真
  upsert；走 ``target_db``）＝ **2 条常数命令**，与给多少个 id 无关。
- ``reconcile_user_dim_incremental``：2 条轻量扫描候选（auth：``(id, updated_at)`` 全量；
  业务：``(user_id, sync_ts)`` 全量）+ 命中时的 2 条（窗口内源读 + upsert）＝ **4 条常数
  命令 / 拍**（无命中则 2 条）。跨库无法 join 比较，改为两次轻量扫描后在进程内求差集，
  故每拍内存为 O(用户数) 的 ``(int, datetime)`` 轻元组（非整行），命令数仍与 N 无关。

新鲜度与 crash-safety 的责任分界（同 A6/A7 盲区：**Profile 单独变更不 bump
``users.updated_at``**，见 task/`member_avatar`/`content_service` 注释）：
- **Event 驱动（单用户，实时）**：A7 已把 ``user.updated/banned/session_revoke`` 投递到
  jobs worker，``invalidate_user_snap`` 在**失效在线缓存的同时**调 ``refresh_user_dim``
  刷新该用户的 dim 行 → 改 User **或改 Profile** 都会把最新源摊进 dim（B0.3 读龄最新）。
  这是**新鲜度主路**，不能只靠 ``updated_at > sync_ts`` 谓词（那会漏 Profile-only 改动）。
- **周期增量对账（批式 crash-safety 网，低频）**：仅按 ``users.updated_at > dim.sync_ts``
  扫（净 User 列改动；Profile-only 已在事件主路兜住）——爬 worker 崩溃/事件丢后的补洞，
  便宜、批式、恒命令数，非新鲜度主路。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import now_iso
from app.db.user_dim import UserDim
from app.modules.auth.models import Profile, User

logger = logging.getLogger("lkm.auth.user_dim_sync")

# 会话对：source=auth 独立库（读 User/Profile），target=业务库（写 user_dim）。
type SessionPair = tuple[AsyncSession, AsyncSession]

# 周期对账的单拍窗口上限：把"待回填的候选 id"分窗成每拍最多这批，下一拍再续（按 user_id
# 递增续窗）→ 命令数 4/拍 与全表行数无关（内存不下爆、单拍时长受控）。事件主路不受此窗约束。
RECONCILE_WINDOW = 500

# 对账 Redis 防交错锁键（镜像 blog reconcile 范式）：多实例/重放时只让一个实例真扫。
_RECONCILE_LOCK = "user_dim:reconcile:lock"


def _rows_for(
    now: datetime.datetime,
    joined: list[tuple[User, Profile | None]],
) -> list[dict[str, Any]]:
    """把 `(User, Profile|None)` 源行折叠成 user_dim 的反范式行 dict（字节镜像）。

    - 列语义严格对齐 B0.1：username/email/account_level/is_locked/created_at/updated_at ←
      users；nickname/role ← profiles（LEFT JOIN 缺失时 None）；is_banned = bool(is_locked)
      （A1 在线缝 snapshot 语义同，供报表对账）；sync_ts = 本次 ETL 写入戳。
    - created_at/updated_at 传源镜像；理论上非空，防御 None 回落 now 保 NOT NULL 不炸。
    """
    rows: list[dict[str, Any]] = []
    for u, p in joined:
        is_locked = bool(u.is_locked)
        rows.append(
            {
                "user_id": u.id,
                "username": u.username,
                "email": u.email,
                "nickname": p.nickname if p else None,
                "role": p.role if p else None,
                "account_level": u.account_level
                if u.account_level is not None
                else "local",
                "is_locked": is_locked,
                "is_banned": is_locked,  # == bool(User.is_locked)，在线缝同义
                "created_at": u.created_at if u.created_at is not None else now,
                "updated_at": u.updated_at if u.updated_at is not None else now,
                "sync_ts": now,
            }
        )
    return rows


async def _load_source_rows(
    source_db: AsyncSession, user_ids: list[uuid.UUID]
) -> list[tuple[User, Profile | None]]:
    """从 **auth 库**批量读给定 user 的源（User 全列 + LEFT JOIN Profile），单条命令，no N+1。

    返回 ``[(User, Profile|None), ...]``（源中已不存在的 id 被 join 丢弃，故只返回存在
    的用户；profile 缺失即 None）。用 OUTER JOIN 一行取两表——**避免** ``selectinload``
    的二次批量查询（维持 sync 读 = 1 条命令）。行序与 DB 返回序一致（不保证输入序）。
    """
    if not user_ids:
        return []
    stmt = (
        select(User, Profile)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(User.id.in_(user_ids))
    )
    res = await source_db.execute(stmt)
    rows: list[tuple[User, Profile | None]] = []
    for user, profile in res.all():
        rows.append((user, profile))
    return rows


async def _upsert_dim_rows(
    target_db: AsyncSession, rows: list[dict[str, Any]]
) -> int:
    """把反范式行批量 upsert 进 **业务库** user_dim（单条 ``ON CONFLICT DO UPDATE``）。

    宽表行 = PK user_id，upsert 把整行重写为最新源，使离线字节副本逐字段对齐源。返回行数。
    """
    if not rows:
        return 0
    # PostgreSQL INSERT … ON CONFLICT DO UPDATE(excluded)：批量整行镜像写回，幂等。
    stmt = pg.insert(UserDim).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[UserDim.user_id],
        set_={
            "username": pg.insert(UserDim).excluded.username,
            "email": pg.insert(UserDim).excluded.email,
            "nickname": pg.insert(UserDim).excluded.nickname,
            "role": pg.insert(UserDim).excluded.role,
            "account_level": pg.insert(UserDim).excluded.account_level,
            "is_locked": pg.insert(UserDim).excluded.is_locked,
            "is_banned": pg.insert(UserDim).excluded.is_banned,
            "created_at": pg.insert(UserDim).excluded.created_at,
            "updated_at": pg.insert(UserDim).excluded.updated_at,
            "sync_ts": pg.insert(UserDim).excluded.sync_ts,
        },
    )
    await target_db.execute(stmt)
    await target_db.flush()
    # 返回"本次抓取的源匹配用户量"= len(rows)。离线条目一律写清(新增=insert、存量=update)，
    # 因只对确有源(join 命中)者写，故每个匹配用户各 1 行 → len(rows) 即本次涉及的 dim 行数。
    # (ty 的 SQLAlchemy stub 缺 rowcount；镜像 rbac/seed 用确定性计数而非结果属性。)
    return len(rows)


async def sync_dim_for_ids(
    source_db: AsyncSession, target_db: AsyncSession, user_ids: list[uuid.UUID]
) -> int:
    """批式 upsert：把 ``user_ids`` 对应的源最新快照摊进 user_dim（跨库双会话）。

    纯批、**命令数恒定 2 条**（1 条 auth 批量源读 + 1 条业务批量 ``ON CONFLICT DO UPDATE``），
    与 N 无关（无 per-row SELECT/UPDATE 循环）。源已不存在的 id 不产生写（不动遗留 dim 行——
    删除治理归上层 report/admin，B0.2 保持表只增镜像）。返回实际 upsert 更新/插入的行数。
    """
    if not user_ids:
        return 0
    joined = await _load_source_rows(source_db, user_ids)
    if not joined:
        return 0
    rows = _rows_for(now_iso(), joined)
    return await _upsert_dim_rows(target_db, rows)


async def refresh_user_dim(
    source_db: AsyncSession, target_db: AsyncSession, *, user_id: uuid.UUID
) -> int:
    """事件驱动的单用户 dim 刷新（新鲜度主路，AUTH user 事件在主路调用）。

    单 id 走同一批式助手（内部单条 auth SELECT + 单条业务 upsert，恒定命令数；对单用户
    不入 any N+1 循环）。与 cache 失效（A7）同源于一条 user 变更事件：在线失效只"打空"
    缓存，dim 需把最新源写进离线副本。返回该用户 dim 行 upsert 行数（源不存在则 0，no-op）。
    """
    return await sync_dim_for_ids(source_db, target_db, [user_id])


async def reconcile_user_dim_incremental(
    source_db: AsyncSession, target_db: AsyncSession, *, window: int = RECONCILE_WINDOW
) -> int:
    """周期增量对账（crash-safety 网，非新鲜度主路）：跨库双会话批扫 + 批量 upsert。

    谓词：``(dim 无行) OR (users.updated_at > dim.sync_ts)``。跨库无法 join，故：
    1) 1 条 **auth** 轻量扫描 ``(User.id, User.updated_at)`` 全量；
    2) 1 条 **业务** 轻量扫描 ``(UserDim.user_id, UserDim.sync_ts)`` 全量；
    3) 进程内求差集得候选，按 ``user_id`` 递增取前 ``window`` 个；
    4) 复用 ``sync_dim_for_ids``（1 源读 + 1 upsert）。
    命令数**恒定 4 条/拍**（无候选则 2 条），与窗口/表行数、已对账数量无关。扫得的两列
    元组是 O(用户数) 轻量内存（非整行），周期性低频，可接受；更大规模再引入水位线。

    局限（有意，非 bug）：Profile-only 变更**不 bump** ``users.updated_at`` → 该谓词天然
    不 catch 它们。但这正是被设计成"安全网"的原因——真实 Profile 变更总是走事件主路
    （A7 ``user.updated`` 在对账看不见的同时已实时把 dim 刷新到最新），本网只补 User 列
    与纯 event 丢/崩场景。返回本拍 upsert 行数。
    """
    # 候选：从未物化的 user 或自 dim 上次写点后源 User 列又变更者。
    src = (
        await source_db.execute(select(User.id, User.updated_at).order_by(User.id))
    ).all()
    if not src:
        return 0
    dim_rows = (await target_db.execute(select(UserDim.user_id, UserDim.sync_ts))).all()
    dim_sync: dict[uuid.UUID, datetime.datetime] = {
        uid: ts for uid, ts in dim_rows
    }
    stale = [
        uid
        for uid, updated_at in src
        if uid not in dim_sync or (updated_at is not None and updated_at > dim_sync[uid])
    ]
    ids = stale[:window]
    if not ids:
        return 0
    return await sync_dim_for_ids(source_db, target_db, ids)


# 任务侧开/消费会话对的统一 seam（镜像 blog reconcile 范式）：默认生产自开两个 realm 会话；
# 测试可 monkeypatch 指向内存/融合库会话，避免触碰真实 DB。事件与周期两条离线写路都经它。


async def _open_session_pair() -> SessionPair:
    """延迟 import 避免模块冷启动拉整棵 db 树（worker 仅消费时再建）。

    返回 ``(auth 源会话, 业务目标会话)``——两库物理分离，绝不合一。
    """
    from app.db.auth_session import new_auth_session
    from app.db.session import new_session

    source = await new_auth_session()
    try:
        target = await new_session()
    except Exception:
        await source.close()
        raise
    return source, target


_session_factory: Callable[[], Awaitable[SessionPair]] = _open_session_pair


async def refresh_user_dim_event(user_id: uuid.UUID) -> int:
    """事件驱动（新鲜度主路）：按单 user 自开双会话刷新 dim 行。

    A7 ``user.updated/banned/session_revoke`` 的 jobs worker 消费在**失效在线缓存的同时**
    调本函数 —— 同一变更事件把最新源写进离线副本（Profile-only 变更也经此被兜，
    users.updated_at 谓词路线看不见 → 这才是新鲜度主路）。自开会话对 + commit，天然离线，
    不进任何在线请求热路径。幂等可重跑。返回该用户 dim 行 upsert 行数。
    """
    source_db, target_db = await _session_factory()
    try:
        n = await refresh_user_dim(source_db, target_db, user_id=user_id)
        await target_db.commit()
        return n
    except Exception:
        await target_db.rollback()
        raise
    finally:
        await source_db.close()
        await target_db.close()


async def reconcile_user_dim_periodic() -> int:
    """自开双会话的周期任务消费口（jobs worker 消费 cron；镜像 blog reconcile 范式）。

    - Redis 锁防自相竞争（多实例只扫一次）；锁不可得 → 本次跳过（下一个周期再来）。
    - 自开 auth+业务会话对 + commit/rollback/close，天然离线，不放任何在线请求热路径。
    - 命令数 = reconcile_user_dim_incremental 的 4 条常数（另有 Redis SET/DEL，非 DB）。
    """
    from app.core.redis import get_redis as _get_redis

    redis = await _get_redis()
    if redis is not None:
        got = await redis.set(_RECONCILE_LOCK, "1", ex=3600, nx=True)
        if not got:
            logger.info("user_dim 对账已被其他实例执行, 本次跳过")
            return 0
    source_db, target_db = await _session_factory()
    try:
        updated = await reconcile_user_dim_incremental(source_db, target_db)
        await target_db.commit()
        return updated
    except Exception:
        await target_db.rollback()
        raise
    finally:
        await source_db.close()
        await target_db.close()
        if redis is not None:
            await redis.delete(_RECONCILE_LOCK)
