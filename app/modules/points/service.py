"""积分服务：balance 原子写 + ledger 幂等流水，reward/spend/transfer/排行榜/每日打卡。"""

import datetime
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    bump_collection_version,
    cache_invalidate,
    cached_read,
    collection_version,
    make_key,
)
from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError
from app.db.base import now_iso
from app.modules.auth.snapshot import get_user_snapshot_batch
from app.modules.points.errors import PointsErr
from app.modules.points.models import (
    Achievement,
    ExchangeItem,
    PointsLedger,
    Task,
    UserAchievement,
    UserBalance,
    UserBehaviorStat,
    UserTaskProgress,
)
from app.modules.points.rules import RULE_DELTAS
from app.modules.points.schemas import (
    AchievementOut,
    ExchangeItemOut,
    LeaderboardEntry,
    LedgerEntry,
    TaskOut,
)


async def ensure_balance(db: AsyncSession, user_id: uuid.UUID) -> UserBalance:
    """惰性取/建用户 balance 行。"""
    row = await db.get(UserBalance, user_id)
    if row is not None:
        return row
    rb = UserBalance(user_id=user_id, balance=0)
    db.add(rb)
    await db.flush()
    return rb


async def _apply_delta(
    db: AsyncSession, user_id: uuid.UUID, delta: int, allow_negative: bool
) -> int:
    """原子增减 balance 并返回变动后的新余额。

    用 ``WHERE balance + delta >= 0`` 约束（允许负时无条件），rowcount==0 表示
    余额不足或用户无记录 → 视为不足。同一事务内该更新对并发安全。
    """
    stmt = sa_update(UserBalance).where(UserBalance.user_id == user_id)
    if allow_negative:
        stmt = stmt.values(balance=UserBalance.balance + delta, updated_at=now_iso())
    else:
        stmt = stmt.where(UserBalance.balance + delta >= 0).values(
            balance=UserBalance.balance + delta, updated_at=now_iso()
        )
    result = await db.execute(stmt)
    if (getattr(result, "rowcount", 0) or 0) == 0:
        raise BizError(PointsErr.INSUFFICIENT_BALANCE, "积分余额不足，或账户未初始化")
    row = await db.get(UserBalance, user_id)
    assert row is not None
    return int(row.balance)


async def reward(
    db: AsyncSession,
    user_id: uuid.UUID,
    delta: int,
    reason: str,
    ref_type: str,
    ref_id: str,
    *,
    allow_negative: bool = False,
) -> LedgerEntry:
    """发放/扣减积分（原子、幂等）。delta 为负表扣分（处罚），allow_negative 放开余额下限。

    幂等：同 (user_id, ref_type, ref_id) 已发过→delta 一致则跳过返回已有流水，
    不一致抛 DUPLICATE_REWARD。返回本次（或既有）流水。
    """
    existing = await db.scalar(
        select(PointsLedger.id).where(
            PointsLedger.user_id == user_id,
            PointsLedger.ref_type == ref_type,
            PointsLedger.ref_id == ref_id,
        )
    )
    if existing is not None:
        entry = (
            (await db.execute(select(PointsLedger).where(PointsLedger.id == existing)))
            .scalars()
            .first()
        )
        if entry is not None and entry.delta == delta:
            return LedgerEntry.model_validate(entry)
        raise BizError(PointsErr.DUPLICATE_REWARD)

    await ensure_balance(db, user_id)
    balance_after = await _apply_delta(db, user_id, delta, allow_negative)
    entry = PointsLedger(
        user_id=user_id,
        delta=delta,
        balance_after=balance_after,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.add(entry)
    # 用 savepoint 承载插入：并发撞 (user, ref_type, ref_id) 唯一约束时只回滚此
    # savepoint，而非 db.rollback() 整事务——否则会连带回滚调用方在本事务里的其它
    # 未提交写（如 do_checkin 的 checkin_streak/last_checkin_date），导致部分丢写。
    sp = await db.begin_nested()
    try:
        await db.flush()
        await sp.commit()
    except IntegrityError:
        await sp.rollback()
        # 并发 insert 被唯一约束拦截 → 视为已发放（幂等）。重取既有流水返回；
        # 其 delta 应与本次一致，不一致属异常抛 DUPLICATE_REWARD。
        row = (
            (
                await db.execute(
                    select(PointsLedger).where(
                        PointsLedger.user_id == user_id,
                        PointsLedger.ref_type == ref_type,
                        PointsLedger.ref_id == ref_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is not None and row.delta == delta:
            return LedgerEntry.model_validate(row)
        raise BizError(PointsErr.DUPLICATE_REWARD) from None
    await bump_collection_version("points")
    await cache_invalidate(make_key("points:balance", user_id))
    return LedgerEntry.model_validate(entry)


async def spend(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: int,
    reason: str,
    ref_type: str,
    ref_id: str,
) -> LedgerEntry:
    """消费积分（余额不足拒）。amount>0。"""
    if amount <= 0:
        raise BizError(PointsErr.INSUFFICIENT_BALANCE, "消费金额须为正")
    return await reward(db, user_id, -amount, reason, ref_type, ref_id)


async def transfer(
    db: AsyncSession,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    amount: int,
    reason: str,
    ref_type: str,
    ref_id: str,
) -> tuple[LedgerEntry, LedgerEntry]:
    """1:1 原子转账：from 扣 + to 加，两笔流水共享 (ref_type, ref_id) 实现幂等。

    单事务内完成；任一失败（如 from 余额不足）整体回滚，不产生部分流水。
    """
    if amount <= 0:
        raise BizError(PointsErr.INSUFFICIENT_BALANCE, "转账金额须为正")
    if from_id == to_id:
        raise BizError(PointsErr.INSUFFICIENT_BALANCE, "不能转账给自己")
    await ensure_balance(db, from_id)
    await ensure_balance(db, to_id)
    from_after = await _apply_delta(db, from_id, -amount, allow_negative=False)
    to_after = await _apply_delta(db, to_id, amount, allow_negative=True)
    out_entry = PointsLedger(
        user_id=from_id,
        delta=-amount,
        balance_after=from_after,
        reason="transfer_out",
        ref_type=ref_type,
        ref_id=ref_id,
    )
    in_entry = PointsLedger(
        user_id=to_id,
        delta=amount,
        balance_after=to_after,
        reason="transfer_in",
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.add(out_entry)
    db.add(in_entry)
    await db.flush()
    await bump_collection_version("points")
    await cache_invalidate(make_key("points:balance", from_id))
    await cache_invalidate(make_key("points:balance", to_id))
    return LedgerEntry.model_validate(out_entry), LedgerEntry.model_validate(in_entry)


async def get_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    """取用户当前余额（读缓存；缺失按 0）。"""

    async def load() -> int:
        existing = await db.scalar(
            select(UserBalance.balance).where(UserBalance.user_id == user_id)
        )
        if existing is None:
            return 0
        return int(existing)

    return await cached_read(make_key("points:balance", user_id), 60, load)


async def list_ledger(
    db: AsyncSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[LedgerEntry]:
    """分页列出用户的积分流水（新→旧）。"""
    total = (
        await db.scalar(
            select(func.count(PointsLedger.id)).where(PointsLedger.user_id == user_id)
        )
        or 0
    )
    stmt = (
        select(PointsLedger)
        .where(PointsLedger.user_id == user_id)
        .order_by(PointsLedger.id.desc())
        .offset(paginate_offset(page, limit))
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    items = [LedgerEntry.model_validate(r) for r in rows]
    return PageData(
        items=items, total=total, page=page, pages=paginate_pages(total, limit)
    )


def _title_from_keys(unlocked: set[str]) -> str:
    """按已解锁成就 key 集合合成稳定 title key（前端 i18n contributionData.leaderboard.titles.*）。

    优先级：a7→hardcore(硬核答主) > a12→columnAuthor(专栏作者) > a8→fileExpert(文件达人)，
    否则默认 active。
    """
    if "a7" in unlocked:
        return "hardcore"
    if "a12" in unlocked:
        return "columnAuthor"
    if "a8" in unlocked:
        return "fileExpert"
    return "active"


async def _titles_for(
    db: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """一次 IN 查询批量返回多个用户已解锁成就合成的 title，避免榜上 N+1 查询。"""
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(Achievement.key, UserAchievement.user_id)
            .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
            .where(
                UserAchievement.user_id.in_(user_ids),
                UserAchievement.unlocked.is_(True),
            )
        )
    ).all()
    unlocked_by_user: dict[uuid.UUID, set[str]] = {}
    for key, uid in rows:
        unlocked_by_user.setdefault(uid, set()).add(key)
    return {uid: _title_from_keys(keys) for uid, keys in unlocked_by_user.items()}


async def _fill_titles(db: AsyncSession, items: list[dict[str, Any]]) -> None:
    """就地给榜单 items 每项补 title（一次批量查询），为空列表时直接跳过。"""
    if not items:
        return
    uid_list = [item["user_id"] for item in items]
    titles = await _titles_for(db, uid_list)
    for item in items:
        item["title"] = titles.get(item["user_id"], "active")


async def leaderboard(
    db: AsyncSession,
    offset: int = 0,
    limit: int = 50,
    period: str = "total",
) -> tuple[list[LeaderboardEntry], int]:
    """积分榜（分页）。period ∈ {total, daily, weekly}；缓存（键含 period）。

    total 按 UserBalance 余额降序（仅 balance>0）；daily/weekly 按 points_ledger
    近窗口 delta>0 归并求和降序。每项附 title。

    **分页为全量排序后偏移切片**：缓存整幅有序榜（key 仅 period），再按
    ``[offset, offset+limit)`` 切片，保证排名连续、total = 榜总人数。
    改动后返回 ``(items, total)`` 而非裸 list。
    """

    async def load() -> list[dict[str, Any]]:
        # M3.B S5 拆库：业务库不再含 users/profiles（auth 真值迁 auth realm）。榜单只从
        # **业务 points 表**取裸 int user_id + 分数，身份展示交由 auth 缝
        # ``snapshot.get_user_snapshot_batch``（seam/HTTP 时读 auth realm，绝不查业务 users），
        # 排序（分数降序、同分按 raw 昵称升序 nullsfirst、user_id 稳定）在服务端 Python 完成，
        # 保原先 fused SQL join+ORDER 的排列契约。
        if period == "total":
            raw = (
                (
                    await db.execute(
                        select(UserBalance.user_id, UserBalance.balance).where(
                            UserBalance.balance > 0
                        )
                    )
                ).all()
            )
            rows = [(uid, int(balance), uid, "") for uid, balance in raw]
        elif period in ("daily", "weekly"):
            days = 1 if period == "daily" else 7
            since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
            agg = (
                await db.execute(
                    select(
                        PointsLedger.user_id,
                        func.sum(PointsLedger.delta).label("total"),
                    )
                    .where(
                        PointsLedger.created_at >= since, PointsLedger.delta > 0
                    )
                    .group_by(PointsLedger.user_id)
                )
            ).all()
            # display fallback 对周期榜原为 str(uid)，恒等放兜底表沿用
            rows = [(uid, int(t), uid, str(uid)) for uid, t in agg]
        else:
            raise BizError(PointsErr.INVALID_PERIOD)

        snaps = await get_user_snapshot_batch(
            db, user_ids=[r[0] for r in rows]
        )
        # 计算型行：(user_id, score)；display 逻辑分开，sort 助手不落入缓存
        ranked: list[dict[str, Any]] = []
        for uid, score, _stable, fallback in rows:
            snap = snaps.get(uid)
            # display_name 原 total= nickname or username；snap.username 权威代业务 User
            display = (
                (snap.nickname or snap.username) if snap is not None else fallback
            )
            ranked.append(
                {
                    "user_id": uid,
                    "score": score,
                    "display_name": display or "",
                    "_nick": snap.nickname if snap is not None else None,
                }
            )
        # 主序分数降序；同分按 raw 昵称升序（无昵称 → None 恒等先排，原 nullsfirst）；
        # user_id 最终稳定序（等价原 SQL 三键）。
        ranked.sort(
            key=lambda r: (
                -r["score"],
                r["_nick"] is not None,
                r["_nick"] or "",
                r["user_id"],
            )
        )
        result = [
            {"user_id": r["user_id"], "display_name": r["display_name"], "balance": r["score"]}
            for r in ranked
        ]
        # 批量补齐 title（UserAchievement 属业务 points 表，留在本进程读），与分数一并落缓存。
        await _fill_titles(db, result)
        return result

    ver = await collection_version("points")
    payload = await cached_read(make_key("points:leaderboard", ver, period), 60, load)
    total = len(payload)
    items = [
        LeaderboardEntry.model_validate(item)
        for item in payload[offset : offset + limit]
    ]
    return items, total


# 成就 type → UserBehaviorStat.stats 计数键（与 engine.STAT_TO_ACHIEVEMENT_TYPE 对齐）
_ACH_TYPE_TO_STAT: dict[str, str] = {
    "post_count": "post",
    "featured_count": "featured_count",
    "accepted_answers": "answer_accepted",
    "approved_files": "file_approved",
    "checkin_streak": "checkin_streak",
    "project_count": "project_count",
    "column_articles": "column_articles",
    "like_count": "like",
    "competition_count": "competition",
    "onboarding": "onboarding",
}


async def _read_progress(db: AsyncSession, user_id: uuid.UUID, type_: str) -> int:
    """只读计算某成就类型的当前进度（读 UserBehaviorStat.stats，不写库）。"""
    stat = await db.get(UserBehaviorStat, user_id)
    key = _ACH_TYPE_TO_STAT.get(type_)
    if stat is None or not key:
        return 0
    return int(stat.stats.get(key, 0))


async def list_achievements(
    db: AsyncSession, *, user_id: uuid.UUID | None = None
) -> list[AchievementOut]:
    """成就定义全量 + 当前用户进度（无登录则不显示进度，归默认值）。"""
    achievements = (
        (await db.execute(select(Achievement).order_by(Achievement.sort_order)))
        .scalars()
        .all()
    )
    progress_map: dict[uuid.UUID, tuple[int, bool]] = {}
    if user_id is not None:
        ua_rows = (
            (
                await db.execute(
                    select(UserAchievement).where(UserAchievement.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        for ua in ua_rows:
            progress_map[ua.achievement_id] = (ua.progress, ua.unlocked)
    out: list[AchievementOut] = []
    for a in achievements:
        if a.id in progress_map:
            prog, unlocked = progress_map[a.id]
        else:
            prog = (
                min(await _read_progress(db, user_id, a.type), a.threshold)
                if user_id is not None
                else 0
            )
            unlocked = False
        out.append(
            AchievementOut(
                id=a.id,
                key=a.key,
                category=a.category,
                icon=a.icon,
                name_key=a.name_key,
                desc_key=a.desc_key,
                type=a.type,
                threshold=a.threshold,
                reward_points=a.reward_points,
                sort_order=a.sort_order,
                progress=prog,
                unlocked=unlocked,
            )
        )
    return out


async def list_tasks(
    db: AsyncSession, *, user_id: uuid.UUID | None = None
) -> list[TaskOut]:
    """任务定义全量 + 当前用户今日进度（无登录则默认值）。"""
    tasks = (await db.execute(select(Task).order_by(Task.sort_order))).scalars().all()
    prog_map: dict[uuid.UUID, tuple[int, bool]] = {}
    if user_id is not None:
        today = datetime.date.today().isoformat()
        up_rows = (
            (
                await db.execute(
                    select(UserTaskProgress).where(
                        UserTaskProgress.user_id == user_id,
                        UserTaskProgress.period_date == today,
                    )
                )
            )
            .scalars()
            .all()
        )
        for up in up_rows:
            prog_map[up.task_id] = (up.progress, up.completed)
    out: list[TaskOut] = []
    for t in tasks:
        cur, done = prog_map.get(t.id, (0, False))
        out.append(
            TaskOut(
                id=t.id,
                key=t.key,
                title_key=t.title_key,
                desc_key=t.desc_key,
                category=t.category,
                requirement_count=t.requirement_count,
                reward_points=t.reward_points,
                sort_order=t.sort_order,
                current_progress=cur,
                completed=done,
            )
        )
    return out


async def list_exchange_items(db: AsyncSession) -> list[ExchangeItemOut]:
    """兑换物品定义全量（公开）。"""
    items = (
        (await db.execute(select(ExchangeItem).order_by(ExchangeItem.sort_order)))
        .scalars()
        .all()
    )
    return [
        ExchangeItemOut(
            id=i.id,
            key=i.key,
            name_key=i.name_key,
            desc_key=i.desc_key,
            points_cost=i.points_cost,
            stock=i.stock,
            is_virtual=i.is_virtual,
            sort_order=i.sort_order,
        )
        for i in items
    ]


async def do_checkin(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """每日打卡：幂等（同日已打返回 today_checked=True, earned=0）。

    返回 ``{success, earned, checkin_streak, today_checked}``。非幂等路径推进打卡
    成就（checkin_streak）与打卡任务（t1），并发放 RULE_DELTAS["checkin"] 积分。
    """
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    stat = (
        (
            await db.execute(
                select(UserBehaviorStat)
                .where(UserBehaviorStat.user_id == user_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if stat is None:
        stat = UserBehaviorStat(user_id=user_id, stats={})
        db.add(stat)
        try:
            await db.flush()
        except IntegrityError:
            # 品牌新用户并发首次打卡：with_for_update 仅守既有行，两个连接都看到
            # None 后各自 insert → 后提交者撞 user_behavior_stats.user_id 主键被 409。
            # 此处回滚本次插入，重取既有行（并发方已提交）继续，模型对齐 reward()。
            await db.rollback()
            stat = (
                (
                    await db.execute(
                        select(UserBehaviorStat)
                        .where(UserBehaviorStat.user_id == user_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if stat is None:  # 理论上不可达：刚有 IntegrityError 说明行已存在
                raise
    today_checked = stat.last_checkin_date == today
    if today_checked:
        return {
            "success": True,
            "earned": 0,
            "checkin_streak": stat.checkin_streak,
            "today_checked": True,
        }

    # 连续天数：昨日连打则 +1，否则重置为 1
    stat.checkin_streak = (
        stat.checkin_streak + 1 if stat.last_checkin_date == yesterday else 1
    )
    stat.last_checkin_date = today
    reward_delta = RULE_DELTAS["checkin"]  # 5
    await reward(
        db,
        user_id,
        reward_delta,
        "checkin",
        "checkin",
        f"{user_id}:{today}",
    )

    # 推进打卡成就（checkin_streak）与打卡任务（t1）
    from app.modules.points.engine import _advance_tasks, _recheck_achievements

    # JSON 列 in-place 变更不被追踪，需整列重赋以标记 dirty
    stat.stats = {**stat.stats, "checkin_streak": stat.checkin_streak}
    await _recheck_achievements(db, user_id, "checkin_streak")
    await _advance_tasks(db, user_id, "checkin", today=today)
    return {
        "success": True,
        "earned": reward_delta,
        "checkin_streak": stat.checkin_streak,
        "today_checked": False,
    }
