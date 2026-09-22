"""积分服务：balance 原子写 + ledger 幂等流水，reward/spend/transfer/排行榜/每日打卡。"""

import datetime
import uuid
from typing import Any

from app.core.cache import (
    bump_collection_version,
    cache_invalidate,
    cached_read,
    collection_version,
    make_key,
)
from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError, CommonErr
from app.db.repository import DbSession
from app.modules.points.errors import PointsErr
from app.modules.points.models import (
    PointsLedger,
    UserBalance,
)
from app.modules.points.repository import (
    AchievementRepository,
    ExchangeItemRepository,
    PointsLedgerRepository,
    TaskRepository,
    UserAchievementRepository,
    UserBalanceRepository,
    UserBehaviorStatRepository,
    UserTaskProgressRepository,
)
from app.modules.points.rules import RULE_DELTAS
from app.modules.points.schemas import (
    AchievementOut,
    ExchangeItemOut,
    LeaderboardEntry,
    LedgerEntry,
    TaskOut,
)
from auth.snapshot import get_user_snapshot_batch


async def ensure_balance(db: DbSession, user_id: uuid.UUID) -> UserBalance:
    """惰性取/建用户 balance 行（并发安全）。"""
    repo = UserBalanceRepository(db)
    row = await repo.get(user_id)
    if row is not None:
        return row
    # 品牌新用户并发首次访问：两个请求都看到 None 后各自 insert，后提交者撞 user_id
    # 主键抛 IntegrityError。沿用 do_checkin 对 user_behavior_stats 的写法：
    # ON CONFLICT DO NOTHING 吸收撞键，再回读（本次插入或并发方已提交的那行）。
    await repo.pg_upsert(
        {"user_id": user_id, "balance": 0},
        index_elements=["user_id"],
        do_nothing=True,
    )
    row = await repo.get(user_id)
    assert row is not None  # 理论上不可达：刚插入或并发方已提交
    return row


async def _apply_delta(
    db: DbSession, user_id: uuid.UUID, delta: int, allow_negative: bool
) -> int:
    """原子增减 balance 并返回变动后的新余额。

    用 ``WHERE balance + delta >= 0`` 约束（允许负时无条件），rowcount==0 表示
    余额不足或用户无记录 → 视为不足。同一事务内该更新对并发安全。
    """
    new_balance = await UserBalanceRepository(db).apply_delta(
        user_id, delta, allow_negative
    )
    if new_balance is None:
        raise BizError(PointsErr.INSUFFICIENT_BALANCE, "积分余额不足，或账户未初始化")
    return new_balance


async def reward(
    db: DbSession,
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
    ledger = PointsLedgerRepository(db)
    # 行锁必须先于幂等预检：否则同一 ref 的两个并发投递会双双通过 get_by_ref 的空判、
    # 各做一次 apply_delta，而唯一约束只把其中一条流水 DO NOTHING 掉 → 余额被加两次，
    # 且与流水的 balance_after 不一致。锁住该用户余额行后，「预检 → 变动 → 落流水」
    # 对同一用户串行（不同用户互不影响）。
    await ensure_balance(db, user_id)
    await UserBalanceRepository(db).lock_for_update(user_id)
    existing = await ledger.get_by_ref(user_id, ref_type, ref_id)
    if existing is not None:
        if existing.delta == delta:
            return LedgerEntry.model_validate(existing)
        raise BizError(PointsErr.DUPLICATE_REWARD)

    balance_after = await _apply_delta(db, user_id, delta, allow_negative)
    # 并发撞 (user, ref_type, ref_id) 唯一约束由 ON CONFLICT DO NOTHING 吸收（替原
    # savepoint 插入：不产生异常、不回滚 savepoint，也不污染调用方其它未提交写）。
    await ledger.pg_upsert(
        {
            "user_id": user_id,
            "delta": delta,
            "balance_after": balance_after,
            "reason": reason,
            "ref_type": ref_type,
            "ref_id": ref_id,
        },
        constraint="uq_points_ledger_ref",
        do_nothing=True,
    )
    # 回读本次或并发方已落的流水（幂等语义下二者等价）。
    entry = await ledger.get_by_ref(user_id, ref_type, ref_id)
    if entry is None or entry.delta != delta:
        raise BizError(PointsErr.DUPLICATE_REWARD)
    await bump_collection_version("points")
    await cache_invalidate(make_key("points:balance", user_id))
    return LedgerEntry.model_validate(entry)


async def spend(
    db: DbSession,
    user_id: uuid.UUID,
    amount: int,
    reason: str,
    ref_type: str,
    ref_id: str,
) -> LedgerEntry:
    """消费积分（余额不足拒）。amount>0。"""
    if amount <= 0:
        raise BizError(CommonErr.INVALID_INPUT, "消费金额须为正")
    return await reward(db, user_id, -amount, reason, ref_type, ref_id)


async def transfer(
    db: DbSession,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    amount: int,
    reason: str,
    ref_type: str,
    ref_id: str,
) -> tuple[LedgerEntry, LedgerEntry]:
    """1:1 原子转账：from 扣 + to 加，两笔流水共享 (ref_type, ref_id) 实现幂等。

    幂等按**转出方**的 (from_id, ref_type, ref_id) 判定：流水唯一约束是
    (user_id, ref_type, ref_id)，而两笔行 user_id 不同、无法互相去重，故重放
    （客户端超时重发）时先回读既有对并原样返回，而不是让唯一约束抛成 500。
    单事务内完成；任一失败（如 from 余额不足）整体回滚，不产生部分流水。
    """
    if amount <= 0:
        raise BizError(CommonErr.INVALID_INPUT, "转账金额须为正")
    if from_id == to_id:
        raise BizError(CommonErr.INVALID_INPUT, "不能转账给自己")
    ledger = PointsLedgerRepository(db)
    # 与 reward 同理：幂等预检前先锁双方余额行（否则并发重放会各扣各加一次、流水被唯一约束
    # 撞掉一条 → 两笔余额都错）。按 uuid 排序取锁，避免「A→B 与 B→A」相互等待成死锁。
    await ensure_balance(db, from_id)
    await ensure_balance(db, to_id)
    balance_repo = UserBalanceRepository(db)
    for uid in sorted((from_id, to_id)):
        await balance_repo.lock_for_update(uid)
    existing_out = await ledger.get_by_ref(from_id, ref_type, ref_id)
    if existing_out is not None:
        existing_in = await ledger.get_by_ref(to_id, ref_type, ref_id)
        if (
            existing_in is not None
            and existing_out.delta == -amount
            and existing_in.delta == amount
        ):
            return (
                LedgerEntry.model_validate(existing_out),
                LedgerEntry.model_validate(existing_in),
            )
        raise BizError(PointsErr.DUPLICATE_REWARD)
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
    await PointsLedgerRepository(db).add_all([out_entry, in_entry])
    await bump_collection_version("points")
    await cache_invalidate(make_key("points:balance", from_id))
    await cache_invalidate(make_key("points:balance", to_id))
    return LedgerEntry.model_validate(out_entry), LedgerEntry.model_validate(in_entry)


async def get_balance(db: DbSession, user_id: uuid.UUID) -> int:
    """取用户当前余额（读缓存；缺失按 0）。"""

    async def load() -> int:
        existing = await UserBalanceRepository(db).get_value(user_id)
        if existing is None:
            return 0
        return int(existing)

    return await cached_read(make_key("points:balance", user_id), 60, load)


async def list_ledger(
    db: DbSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[LedgerEntry]:
    """分页列出用户的积分流水（新→旧）。"""
    repo = PointsLedgerRepository(db)
    total = await repo.count(PointsLedger.user_id == user_id)
    rows = await repo.get_many(
        PointsLedger.user_id == user_id,
        order_by=PointsLedger.id.desc(),
        offset=paginate_offset(page, limit),
        limit=limit,
    )
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


async def _titles_for(db: DbSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """一次 IN 查询批量返回多个用户已解锁成就合成的 title，避免榜上 N+1 查询。"""
    if not user_ids:
        return {}
    unlocked_by_user = await AchievementRepository(db).unlocked_keys_for(user_ids)
    return {uid: _title_from_keys(keys) for uid, keys in unlocked_by_user.items()}


async def _fill_titles(db: DbSession, items: list[dict[str, Any]]) -> None:
    """就地给榜单 items 每项补 title（一次批量查询），为空列表时直接跳过。"""
    if not items:
        return
    uid_list = [item["user_id"] for item in items]
    titles = await _titles_for(db, uid_list)
    for item in items:
        item["title"] = titles.get(item["user_id"], "active")


async def leaderboard(
    db: DbSession,
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
            raw = await UserBalanceRepository(db).list_positive()
            rows = [(uid, balance, uid, "") for uid, balance in raw]
        elif period in ("daily", "weekly"):
            days = 1 if period == "daily" else 7
            since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
            agg = await PointsLedgerRepository(db).sum_positive_since(since)
            # display fallback 对周期榜原为 str(uid)，恒等放兜底表沿用
            rows = [(uid, total, uid, str(uid)) for uid, total in agg]
        else:
            raise BizError(PointsErr.INVALID_PERIOD)

        snaps = await get_user_snapshot_batch(db, user_ids=[r[0] for r in rows])
        # 计算型行：(user_id, score)；display 逻辑分开，sort 助手不落入缓存
        ranked: list[dict[str, Any]] = []
        for uid, score, _stable, fallback in rows:
            snap = snaps.get(uid)
            # display_name 原 total= nickname or username；snap.username 权威代业务 User
            display = (snap.nickname or snap.username) if snap is not None else fallback
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
            {
                "user_id": r["user_id"],
                "display_name": r["display_name"],
                "balance": r["score"],
            }
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


def _progress_from_stat(stat: Any, type_: str) -> int:
    """由**已取好**的 UserBehaviorStat 行算某成就类型进度（读 stats，不写库）。

    调用方一次性取 stat 行后循环调用（原先每个成就类型各查一次 = N+1）。
    """
    key = _ACH_TYPE_TO_STAT.get(type_)
    if stat is None or not key:
        return 0
    return int(stat.stats.get(key, 0))


async def list_achievements(
    db: DbSession, *, user_id: uuid.UUID | None = None
) -> list[AchievementOut]:
    """成就定义全量 + 当前用户进度（无登录则不显示进度，归默认值）。"""
    achievements = await AchievementRepository(db).list_ordered()
    progress_map: dict[uuid.UUID, tuple[int, bool]] = {}
    stat: Any = None
    if user_id is not None:
        ua_rows = await UserAchievementRepository(db).list_for_user(user_id)
        for ua in ua_rows:
            progress_map[ua.achievement_id] = (ua.progress, ua.unlocked)
        # 行为统计行整批只需一行：原先在循环里对每个无成就记录的类型各查一次（仓库是
        # select().scalars().first()，没有 identity-map 短路）→ 典型的 N+1
        stat = await UserBehaviorStatRepository(db).get(user_id)
    out: list[AchievementOut] = []
    for a in achievements:
        if a.id in progress_map:
            prog, unlocked = progress_map[a.id]
        else:
            prog = (
                min(_progress_from_stat(stat, a.type), a.threshold)
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
    db: DbSession, *, user_id: uuid.UUID | None = None
) -> list[TaskOut]:
    """任务定义全量 + 当前用户今日进度（无登录则默认值）。"""
    tasks = await TaskRepository(db).list_ordered()
    prog_map: dict[uuid.UUID, tuple[int, bool]] = {}
    if user_id is not None:
        today = datetime.date.today().isoformat()
        up_rows = await UserTaskProgressRepository(db).list_for_user_period(
            user_id, today
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


async def list_exchange_items(db: DbSession) -> list[ExchangeItemOut]:
    """兑换物品定义全量（公开）。"""
    items = await ExchangeItemRepository(db).list_ordered()
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


async def do_checkin(db: DbSession, user_id: uuid.UUID) -> dict:
    """每日打卡：幂等（同日已打返回 today_checked=True, earned=0）。

    返回 ``{success, earned, checkin_streak, today_checked}``。非幂等路径推进打卡
    成就（checkin_streak）与打卡任务（t1），并发放 RULE_DELTAS["checkin"] 积分。
    """
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    stat_repo = UserBehaviorStatRepository(db)
    stat = await stat_repo.get_locked(user_id)
    if stat is None:
        # 品牌新用户并发首次打卡：with_for_update 仅守既有行，两个连接都看到 None 后
        # 各自 insert → 后提交者撞 user_behavior_stats.user_id 主键。此处 ON CONFLICT
        # DO NOTHING 吸收撞键（替原 flush 撞 IntegrityError → db.rollback() 重取路径，
        # 不再整事务回滚），随后行锁回读并发方已提交的行继续。
        await stat_repo.pg_upsert(
            {"user_id": user_id, "stats": {}},
            index_elements=["user_id"],
            do_nothing=True,
        )
        stat = await stat_repo.get_locked(user_id)
        assert stat is not None  # 理论上不可达：刚插入或并发方已提交
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
