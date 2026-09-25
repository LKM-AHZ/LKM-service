"""内容互动计数：写穿为主、Redis 增量链路退居回退通道（B3 重构 M6.10）。

三项计数的**真相源是明细表**，``content_items`` 上的计数列是派生值：

- ``like_count``     ← ``content_likes`` 行数
- ``comment_count``  ← ``content_comments`` 行数
- ``bookmark_count`` ← ``interaction_favorites`` 行数

**写穿（默认，``LKM_COUNTERS_WRITE_THROUGH=true``）**：写路径与明细同事务原子 UPDATE 计数列，
派生列与明细强一致——读数是真值，无 flush 窗口偏差，也不再需要「DB 值 + pending」的近似合成。

**回退通道（开关关闭）**：M6.10 的 write-behind —— 差值记进 Redis（``core.counters``）、
``flush_counters`` 周期落库。保留它只为可回滚，不是默认路径。

``reconcile_counts`` 两条路径下都保留：按明细 ``COUNT(*)`` 重算并修正偏差（**可证伪**：
连续两次对账，第二次 ``affected == 0``），作为历史脏值与异常路径的兜底收敛。

**不纳入本链路的计数**：``view_count``（浏览数）。它没有可重算的真相源——浏览明细
``interaction_view_logs`` 是「每用户每内容一行」的 upsert，行数与累计浏览次数不可换算，
对不上账。故 ``view_count`` 维持原有原子 UPDATE 直改 DB，登记于路线图 §8。

跨模块只读：``bookmark_count`` 的明细在 interaction 域，故 import
``interaction.models``（已在 pyproject 契约中精确豁免）。
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import counters
from app.core.config import settings
from app.core.err import BizError
from app.core.metrics import counts_reconcile_repeated_total
from app.modules.content.errors import ContentErr
from app.modules.content.models import ContentComment, ContentItem, ContentLike
from app.modules.interaction.models import InteractionFavorite

logger = logging.getLogger(__name__)

# 计数列白名单：字段名 → ORM 列（同时用于 UPDATE 表达式构造）
_COLUMNS: dict[str, sa.Column[int]] = {
    "like_count": ContentItem.like_count,
    "comment_count": ContentItem.comment_count,
    "bookmark_count": ContentItem.bookmark_count,
}

# 真相源：字段名 → 明细表的 content_id 列
_DETAIL_SOURCES: dict[str, sa.Column[uuid.UUID]] = {
    "like_count": ContentLike.content_id,
    "comment_count": ContentComment.content_id,
    "bookmark_count": InteractionFavorite.content_id,
}


async def write_through_bump(
    db: AsyncSession, item_id: uuid.UUID, field: str, delta: int
) -> int:
    """**写穿主路径**：原子 UPDATE 直改 DB（下限 0），与业务明细同事务、返回新值。

    计数列的真相源是明细表（见模块 docstring），写穿让派生列与明细强一致——读路径不再需要
    「DB 值 + 未落库差值」的近似合成，也不存在 flush 窗口内的偏小读数。

    行锁由 UPDATE 自身承担：并发对同一行的 +1/-1 串行化，不丢增量（``greatest(...,0)`` 兜底
    下限）。行不存在（含已硬删）抛 ``CONTENT_NOT_FOUND``，使调用方事务整体回滚。
    """
    col = _COLUMNS[field]
    result = await db.execute(
        sa.update(ContentItem)
        .where(ContentItem.id == item_id)
        .values(**{field: func.greatest(col + delta, 0)})
        .returning(col)
    )
    row = result.first()
    if row is None:
        raise BizError(ContentErr.CONTENT_NOT_FOUND)
    return int(row[0])


async def bump_content_counter(
    db: AsyncSession, item_id: uuid.UUID, field: str, delta: int
) -> int:
    """记一次计数增减，返回**权威读数**。

    - ``counters_write_through``（默认开）：与明细同事务原子 UPDATE，返回值为 DB 真值。
    - 关：回退 M6.10 的 Redis 增量链路（Redis 可用则只记增量、由 flush 收敛；不可用则
      原路原子 UPDATE）。
    """
    if field not in _COLUMNS:
        raise ValueError(f"unsupported counter field: {field!r}")

    if settings.counters_write_through:
        return await write_through_bump(db, item_id, field, delta)

    # ---- 回退路径（M6.10 的 write-behind）----
    # 先确认内容行存在，**再**写 Redis：反过来（先 INCR 后校验）会在内容不存在/已删时
    # 留下无人认领的增量键——本调用抛 CONTENT_NOT_FOUND，但那个 +1 不会被回滚，
    # flush 时 UPDATE 命中 0 行而被静默丢弃，后续 id 复用还会继承这个陈旧值。
    col = _COLUMNS[field]
    base = await db.scalar(select(col).where(ContentItem.id == item_id))
    if base is None:
        raise BizError(ContentErr.CONTENT_NOT_FOUND)

    if await counters.bump_counter(field, item_id, delta):
        return int(base) + await counters.pending_delta(field, item_id)

    return await write_through_bump(db, item_id, field, delta)


async def read_count(db: AsyncSession, item_id: uuid.UUID, field: str) -> int:
    """即时读数：写穿模式下即 DB 计数列；回退模式下是「DB 值 + 未落库差值」。"""
    # 与 bump_content_counter 同口径校验：否则未支持字段名会以裸 KeyError 冒成 500
    if field not in _COLUMNS:
        raise ValueError(f"unsupported counter field: {field!r}")
    col = _COLUMNS[field]
    base = await db.scalar(select(col).where(ContentItem.id == item_id))
    if base is None:
        raise BizError(ContentErr.CONTENT_NOT_FOUND)
    if settings.counters_write_through:
        return int(base)
    return int(base) + await counters.pending_delta(field, item_id)


async def flush_counters(db: AsyncSession) -> int:
    """把 Redis 里的待落库差值刷进计数列，返回被更新的行数。

    取走（``GETDEL``）即清零：若本次 DB 写入失败，差值丢失而非重复计入（宁少不重），
    偏差由 ``reconcile_counts`` 收敛。
    """
    drained = await counters.drain_counters()
    if not drained:
        return 0

    applied = 0
    for (field, item_id), delta in drained.items():
        if field not in _COLUMNS or not delta:
            continue
        col = _COLUMNS[field]
        result = await db.execute(
            sa.update(ContentItem)
            .where(ContentItem.id == item_id)
            .values(**{field: func.greatest(col + delta, 0)})
        )
        affected = int(result.rowcount or 0)
        if affected == 0:
            # 目标行已不存在（内容被硬删）：该增量被丢弃，原本静默无痕
            logger.warning(
                "flush_counters 未命中行，丢弃增量 field=%s item=%s delta=%s",
                field,
                item_id,
                delta,
            )
        applied += affected
    return applied


async def _has_pending_delta(item_id: uuid.UUID) -> bool:
    """该行三项计数是否还有未落库的 Redis 差值（Redis 不可用时恒 False）。"""
    for field in _COLUMNS:
        if await counters.pending_delta(field, item_id):
            return True
    return False


# 上一轮被修正的 key：本轮若再次命中同一 key，即为「多轮 diff 不降反升」的震荡信号。
_last_corrected_ids: set[uuid.UUID] = set()


def reset_reconcile_oscillation_state() -> None:
    """清空震荡检测的跨轮状态（测试逐例隔离用；生产不需要调用）。"""
    _last_corrected_ids.clear()


def _record_oscillation(corrected: set[uuid.UUID]) -> None:
    """对「连续两轮都需修正」的 key 发震荡告警并推进跨轮状态。

    蓝图 §5.6 要求判定震荡。这里只**报告**、不自动暂停该 key 的对账——自动暂停会让它永久
    失去兜底修正（计数越漂越远且无人修），风险大于收益；介入方式是运维按日志里的 id 排查
    写方向是否被破坏。
    """
    repeated = corrected & _last_corrected_ids
    if repeated:
        counts_reconcile_repeated_total.inc(len(repeated))
        shown = ", ".join(sorted(str(i) for i in repeated))[:500]
        logger.warning(
            "对账震荡：%d 个计数 key 连续两轮都需修正（写方向可能被破坏）: %s",
            len(repeated),
            shown,
        )
    _last_corrected_ids.clear()
    _last_corrected_ids.update(corrected)


async def reconcile_counts(
    db: AsyncSession, batch_size: int = 500, *, only_unconverged: bool = True
) -> tuple[int, int]:
    """按明细表重算三项计数并修正偏差，返回 ``(scanned, affected)``。

    以 ``id`` 键集分窗（每窗一条聚合查询 + 至多 N 条修正 UPDATE），内存与命令数有界。
    只更新**确有偏差**的行，并把 ``counts_reconciled_at`` 记为该行被修正的时刻——
    因此连续两次对账第二次 ``affected == 0``，收敛可证伪（见 tests/test_counters.py）。
    例外：该行若仍有未落库的 Redis 增量则本拍跳过（否则会与随后的 ``flush_counters``
    叠加成超调），留待下一拍收敛。

    ``only_unconverged=True``（默认，**增量拍**）：只扫 ``counts_reconciled_at IS NULL`` 的行
    （尚未被对账确认过）。扫过且**无偏差**的行也会被一次性批量打上 ``counts_reconciled_at``，
    让「已收敛」真正生效——否则未漂移的行会每拍重扫，对账自身空转（蓝图 §5.6「收敛终止条件」）。
    **漂移兜底由日级全量负责**：传 ``only_unconverged=False`` 时无视标记、全表重扫，
    用于捕获「标记之后又被改坏」的行（蓝图的两级设计：秒级增量 + 日级全量）。

    **震荡检测**：连续两轮都需修正同一个 key → 计入 ``counts_reconcile_repeated_total``
    并告警（见 :func:`_record_oscillation`）。
    """
    scanned = 0
    affected = 0
    corrected: set[uuid.UUID] = set()
    converged: set[uuid.UUID] = set()
    # 键集水位：uuid 主键无 0 起点，用最小值 nil UUID 作首窗下界（PG uuid 按字节序，
    # UUID(int=0) 全零即最小），后续直接取回上行 uuid，无需 int() 转换。
    last_id = uuid.UUID(int=0)
    while True:
        real_cols = [
            select(func.count())
            .select_from(ContentLike)
            .where(ContentLike.content_id == ContentItem.id)
            .scalar_subquery()
            .label("real_like"),
            # 口径与在线计数一致：**不含已软删评论**。若此处不滤，软删评论会被对账
            # 反复算回来，与「软删时 comment_count 减一」互相覆盖、永久震荡。
            select(func.count())
            .select_from(ContentComment)
            .where(
                ContentComment.content_id == ContentItem.id,
                ContentComment.deleted_at.is_(None),
            )
            .scalar_subquery()
            .label("real_comment"),
            select(func.count())
            .select_from(InteractionFavorite)
            .where(InteractionFavorite.content_id == ContentItem.id)
            .scalar_subquery()
            .label("real_bookmark"),
        ]
        where_conds = [ContentItem.id > last_id]
        if only_unconverged:
            # 只扫「还没被本轮之前的对账确认收敛过」的行。**刻意不叠加 updated_at 比较**：
            # 模型的 onupdate 用的是 Python 侧 now()（晚于下方标记用的事务级 now()），
            # 「counts_reconciled_at < updated_at」会恒真 → 每拍重扫、增量失效。
            # 代价：标记后**又发生漂移**的行要等日级全量兜底（only_unconverged=False）——
            # 这正是蓝图的两级设计（秒级增量 + 日级全量）。
            where_conds.append(ContentItem.counts_reconciled_at.is_(None))
        rows = (
            await db.execute(
                select(
                    ContentItem.id,
                    ContentItem.like_count,
                    ContentItem.comment_count,
                    ContentItem.bookmark_count,
                    *real_cols,
                )
                .where(*where_conds)
                .order_by(ContentItem.id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            break

        for row in rows:
            item_id = row[0]
            current = (int(row[1]), int(row[2]), int(row[3]))
            real = (int(row[4]), int(row[5]), int(row[6]))
            if real == current:
                converged.add(item_id)  # 已收敛：本拍标记，下一拍不再重复扫它
                continue
            # 还有未落库的 Redis 差值时不纠正：明细 COUNT 已包含这些增量，
            # 此刻写回真值后，随后的 flush_counters 会把同一增量再加一遍（超调）。
            # 等下一拍（flush 之后）再收敛，漂移只是延后一拍，不是永久。
            if await _has_pending_delta(item_id):
                continue
            await db.execute(
                sa.update(ContentItem)
                .where(ContentItem.id == item_id)
                .values(
                    like_count=real[0],
                    comment_count=real[1],
                    bookmark_count=real[2],
                    counts_reconciled_at=func.now(),
                    # 显式保持 updated_at 不动：对账是**内部收敛**，不该算作内容被编辑。
                    # 不这样写的话，模型的 onupdate（Python 侧 now，晚于下面这个事务级
                    # now()）会把 updated_at 顶到 counts_reconciled_at 之后，让增量谓词
                    # 「counts_reconciled_at < updated_at」恒真 → 每拍重扫，增量失效。
                    updated_at=ContentItem.updated_at,
                )
            )
            corrected.add(item_id)
            affected += 1

        scanned += len(rows)
        last_id = rows[-1][0]

    if converged:
        # 一次性批量标记「本拍确认已收敛」（摊薄写，不是每拍全表逐行），
        # 让增量谓词在下一拍跳过它们。同样显式保持 updated_at（见上条注释）。
        await db.execute(
            sa.update(ContentItem)
            .where(ContentItem.id.in_(converged))
            .values(
                counts_reconciled_at=func.now(),
                updated_at=ContentItem.updated_at,
            )
        )
    _record_oscillation(corrected)
    return scanned, affected
