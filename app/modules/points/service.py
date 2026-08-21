"""积分服务：balance 原子写 + ledger 幂等流水，reward/spend/transfer/排行榜/每日打卡。"""

import datetime
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
from app.core.err import BizError
from app.db.models import (
    Achievement,
    ExchangeItem,
    PointsLedger,
    Profile,
    Task,
    User,
    UserAchievement,
    UserBalance,
    UserBehaviorStat,
    UserTaskProgress,
    now_iso,
)
from app.modules.common import PageData, paginate_offset, paginate_pages
from app.modules.points.errors import PointsErr
from app.modules.points.rules import RULE_DELTAS
from app.modules.points.schemas import (
    AchievementOut,
    ExchangeItemOut,
    LeaderboardEntry,
    LedgerEntry,
    TaskOut,
)


async def ensure_balance(db: AsyncSession, user_id: int) -> UserBalance:
    """惰性取/建用户 balance 行。"""
    row = await db.get(UserBalance, user_id)
    if row is not None:
        return row
    rb = UserBalance(user_id=user_id, balance=0)
    db.add(rb)
    await db.flush()
    return rb


async def _apply_delta(
    db: AsyncSession, user_id: int, delta: int, allow_negative: bool
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
    user_id: int,
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
    try:
        await db.flush()
    except IntegrityError:
        # 同 (user, ref_type, ref_id) 的并发 insert 被唯一约束拦截 → 视为已发放（幂等）。
        # 回滚本次插入，返回既有流水（其 delta 应与本次一致；不一致属异常，抛 DUPLICATE_REWARD）。
        await db.rollback()
        existing = await db.scalar(
            select(PointsLedger.id).where(
                PointsLedger.user_id == user_id,
                PointsLedger.ref_type == ref_type,
                PointsLedger.ref_id == ref_id,
            )
        )
        row = None
        if existing is not None:
            row = (
                (
                    await db.execute(
                        select(PointsLedger).where(PointsLedger.id == existing)
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
    user_id: int,
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
    from_id: int,
    to_id: int,
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


async def get_balance(db: AsyncSession, user_id: int) -> int:
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
    db: AsyncSession, user_id: int, page: int = 1, limit: int = 20
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


async def _titles_for(db: AsyncSession, user_ids: list[int]) -> dict[int, str]:
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
    unlocked_by_user: dict[int, set[str]] = {}
    for key, uid in rows:
        unlocked_by_user.setdefault(uid, set()).add(key)
    return {uid: _title_from_keys(keys) for uid, keys in unlocked_by_user.items()}


async def _fill_titles(db: AsyncSession, items: list[dict[str, Any]]) -> None:
    """就地给榜单 items 每项补 title（一次批量查询），为空列表时直接跳过。"""
    if not items:
        return
    uid_list = [int(item["user_id"]) for item in items]
    titles = await _titles_for(db, uid_list)
    for item in items:
        item["title"] = titles.get(int(item["user_id"]), "active")


async def leaderboard(
    db: AsyncSession, limit: int = 50, period: str = "total"
) -> list[LeaderboardEntry]:
    """积分榜。period ∈ {total, daily, weekly}；缓存（键含 period + collection_version）。

    total 按 UserBalance 余额降序（仅 balance>0）；daily/weekly 按 points_ledger
    近窗口 delta>0 归并求和降序。每项附 title。
    """

    async def load() -> list[dict[str, Any]]:
        if period == "total":
            rows = (
                await db.execute(
                    select(User, Profile.nickname, UserBalance.balance)
                    .outerjoin(Profile, Profile.user_id == User.id)
                    .join(UserBalance, UserBalance.user_id == User.id)
                    .where(UserBalance.balance > 0)
                    # 主序余额降序；同余额按 display_name（昵称）升序，昵称可空，
                    # nullsfirst 让无昵称者（退化为 username）排在前面；User.id 作最终稳定序。
                    .order_by(
                        UserBalance.balance.desc(),
                        Profile.nickname.asc().nullsfirst(),
                        User.id.asc(),
                    )
                    .limit(limit)
                )
            ).all()
            result: list[dict[str, Any]] = []
            for user, nickname, balance in rows:
                result.append(
                    {
                        "user_id": user.id,
                        "display_name": nickname or user.username or "",
                        "balance": int(balance or 0),
                    }
                )
            # 一次批量查询补齐 title，与 balance 一并落入缓存，避免榜上 N+1 且保证一致
            await _fill_titles(db, result)
            return result
        if period in ("daily", "weekly"):
            days = 1 if period == "daily" else 7
            since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
            agg_rows = (
                await db.execute(
                    select(
                        User.id,
                        Profile.nickname,
                        func.sum(PointsLedger.delta).label("total"),
                    )
                    .join(PointsLedger, PointsLedger.user_id == User.id)
                    .outerjoin(Profile, Profile.user_id == User.id)
                    .where(PointsLedger.created_at >= since, PointsLedger.delta > 0)
                    .group_by(User.id, Profile.nickname)
                    .order_by(
                        func.sum(PointsLedger.delta).desc(),
                        Profile.nickname.asc().nullsfirst(),
                        User.id.asc(),
                    )
                    .limit(limit)
                )
            ).all()
            result = [
                {
                    "user_id": uid,
                    "display_name": nickname or str(uid),
                    "balance": int(total),
                }
                for uid, nickname, total in agg_rows
            ]
            await _fill_titles(db, result)
            return result
        raise BizError(PointsErr.INVALID_PERIOD)

    ver = await collection_version("points")
    payload = await cached_read(
        make_key("points:leaderboard", ver, period, limit), 60, load
    )
    return [LeaderboardEntry.model_validate(item) for item in payload]


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


async def _read_progress(db: AsyncSession, user_id: int, type_: str) -> int:
    """只读计算某成就类型的当前进度（读 UserBehaviorStat.stats，不写库）。"""
    stat = await db.get(UserBehaviorStat, user_id)
    key = _ACH_TYPE_TO_STAT.get(type_)
    if stat is None or not key:
        return 0
    return int(stat.stats.get(key, 0))


async def list_achievements(
    db: AsyncSession, *, user_id: int | None = None
) -> list[AchievementOut]:
    """成就定义全量 + 当前用户进度（无登录则不显示进度，归默认值）。"""
    achievements = (
        (await db.execute(select(Achievement).order_by(Achievement.sort_order)))
        .scalars()
        .all()
    )
    progress_map: dict[int, tuple[int, bool]] = {}
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
            prog = min(await _read_progress(db, user_id or 0, a.type), a.threshold)
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


async def list_tasks(db: AsyncSession, *, user_id: int | None = None) -> list[TaskOut]:
    """任务定义全量 + 当前用户今日进度（无登录则默认值）。"""
    tasks = (await db.execute(select(Task).order_by(Task.sort_order))).scalars().all()
    prog_map: dict[int, tuple[int, bool]] = {}
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


async def do_checkin(db: AsyncSession, user_id: int) -> dict:
    """每日打卡：幂等（同日已打返回 today_checked=True, earned=0）。

    返回 ``{success, earned, checkin_streak, today_checked}``。非幂等路径推进打卡
    成就（checkin_streak）与打卡任务（t1），并发放 RULE_DELTAS["checkin"] 积分。
    """
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    stat = (
        (
            await db.execute(
                select(UserBehaviorStat).where(UserBehaviorStat.user_id == user_id)
            )
        )
        .scalars()
        .first()
    )
    if stat is None:
        stat = UserBehaviorStat(user_id=user_id, stats={})
        db.add(stat)
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
