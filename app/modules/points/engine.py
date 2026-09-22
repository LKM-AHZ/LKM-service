"""积分事件消费副作用：更新行为计数 + 解锁成就 + 推进当日任务并达标另发奖励分。"""

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.points.models import (
    Achievement,
    Task,
    UserAchievement,
    UserBehaviorStat,
    UserTaskProgress,
)
from app.modules.points.repository import (
    UserAchievementRepository,
    UserTaskProgressRepository,
)
from app.modules.points.service import reward

# 事件 → 行为统计计数键（写入 UserBehaviorStat.stats）
EVENT_STAT_KEY: dict[str, str] = {
    "post": "post",
    "comment": "comment",
    "like": "like",
    "file_approved": "file_approved",
    "answer_accepted": "answer_accepted",
    "competition": "competition",
}

# 行为计数键 → 成就 type 的匹配（用于重算成就进度）
STAT_TO_ACHIEVEMENT_TYPE: dict[str, str] = {
    "post": "post_count",
    "like": "like_count",
    "file_approved": "approved_files",
    "answer_accepted": "accepted_answers",
    "checkin_streak": "checkin_streak",
    "competition": "competition_count",
}

# 事件 → 任务匹配 category（推进当日任务）
EVENT_TASK_KEY: dict[str, str] = {
    "post": "post",
    # 评论不入任何任务：仅发帖(post)推进发表任务，回帖不再 feed
    "answer_accepted": "answer",
    "like": "like",
    "file_approved": "file_upload",
    "checkin": "checkin",
    "competition": "competition",
}


def _today() -> str:
    """当前日期（本地时区，YYYY-MM-DD）。"""
    return datetime.date.today().isoformat()


async def _get_or_create_stats(
    db: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False
) -> UserBehaviorStat:
    """惰性取/建用户行为统计行。

    ``for_update=True`` 用于将要读改写 ``stats`` JSON 的写路径：对既有行加行锁，
    与 ``service.do_checkin`` 的 ``SELECT ... FOR UPDATE`` 对齐，避免并发读改写
    JSON 列时互相覆盖丢更新。
    """
    if for_update:
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
    else:
        stat = await db.get(UserBehaviorStat, user_id)
    if stat is None:
        stat = UserBehaviorStat(user_id=user_id, stats={})
        # 用 savepoint 承载插入；并发撞主键只回滚本 savepoint，而非 db.rollback()
        # 整事务——否则会连带回滚调用方本事务里未提交的其它写（如 worker 里 reward()
        # 已写入的 ledger 流水），导致发分后续又因重试被跳过，数据不一致。
        # db.add 必须在 begin_nested() **之后**：savepoint 回滚只 expunge 快照之后新增的
        # 对象，若在快照前 add，回滚后该 pending 行仍留在 session.new，后续 flush/autoflush
        # 会再 INSERT 撞主键，抛未捕获的 IntegrityError 拖垮整个事务。
        sp = await db.begin_nested()
        try:
            db.add(stat)
            await db.flush()
            await sp.commit()
        except IntegrityError:
            await sp.rollback()
            updated = await db.get(UserBehaviorStat, user_id)
            if updated is None:
                raise  # 理论上不会发生；保守上抛
            return updated
    return stat


async def _bump_count(db: AsyncSession, user_id: uuid.UUID, key: str) -> int:
    """把某行为计数字段 +1，返回新值。

    JSON 列的 in-place 变更不会被 SQLAlchemy 追踪，需整列重赋以标记 dirty。
    先锁行再读改写，防止与 do_checkin 等并发写 stats 时互覆盖丢更新。
    """
    stat = await _get_or_create_stats(db, user_id, for_update=True)
    cur = int(stat.stats.get(key, 0))
    stat.stats = {**stat.stats, key: cur + 1}
    return cur + 1


# stats JSON 里记录已消费 (event, ref_id) 的保留键（幂等去重）
_PROCESSED_KEY = "processed_events"


async def _already_processed(
    db: AsyncSession,
    user_id: uuid.UUID,
    event: str,
    ref_id: str,
    *,
    namespace: str = "legacy",
) -> bool:
    """该 (namespace, event, ref_id) 是否已消费过？仅由各副作用入口经行锁调用。

    ``namespace``：M4 points 拆订阅后各订阅用独立命名空间（stats/tasks），避免同一事件
    被一个订阅标记后其它订阅误跳过；保留整入口用 ``legacy`` 命名空间。
    """
    stat = await _get_or_create_stats(db, user_id, for_update=True)
    processed = stat.stats.get(_PROCESSED_KEY)
    return isinstance(processed, dict) and f"{namespace}:{event}:{ref_id}" in processed


async def _mark_processed(
    db: AsyncSession,
    user_id: uuid.UUID,
    event: str,
    ref_id: str,
    *,
    namespace: str = "legacy",
) -> None:
    """记录该 (namespace, event, ref_id) 已消费，与副作用同一事务原子落库。"""
    stat = await _get_or_create_stats(db, user_id, for_update=True)
    processed = stat.stats.get(_PROCESSED_KEY)
    if not isinstance(processed, dict):
        processed = {}
    processed = {**processed, f"{namespace}:{event}:{ref_id}": True}
    stat.stats = {**stat.stats, _PROCESSED_KEY: processed}


async def apply_event_side_effects(
    db: AsyncSession,
    user_id: uuid.UUID,
    event: str,
    ref_id: str,
    *,
    today: str | None = None,
) -> None:
    """事件副作用主入口。today 可注入，便于测试对齐日期。

    幂等：以 ``UserBehaviorStat`` 行锁内的 ``processed_events`` 标记去重。消息可能
    在「commit 成功但 ack 前崩溃/断连」后重投：此时 reward() 靠 ledger 唯一约束跳过，
    但此处若不幂等，行为计数/成就/任务进度会重复累计。副作用与该标记同一事务
    提交，故重投读到已提交标记即整体跳过；部分失败回滚则重投干净重做。
    """
    stat_key = EVENT_STAT_KEY.get(event)
    task_key = EVENT_TASK_KEY.get(event)
    # 未知/未映射事件：无副作用可做，保持原「不触碰 UserBehaviorStat」语义。
    if not stat_key and not task_key:
        return
    if await _already_processed(db, user_id, event, ref_id):
        return
    await _mark_processed(db, user_id, event, ref_id)
    tday = today or _today()
    # 1. 行为计数 + 成就重算
    if stat_key:
        await _bump_count(db, user_id, stat_key)
        await _recheck_achievements(db, user_id, stat_key)
    # 2. 每日任务推进
    await _advance_tasks(db, user_id, event, today=tday)


async def apply_stats_side_effects(
    db: AsyncSession, user_id: uuid.UUID, event: str, ref_id: str
) -> None:
    """points-stats 订阅入口：仅行为计数 + 成就重算（独立幂等命名空间 stats）。

    M4 把 points 拆为 reward/stats/tasks 三订阅扇出：本函数只做统计维度，与 tasks 订阅
    同事件各跑各的互不影响。幂等键带 ``stats:`` 命名空间，与整入口 legacy 命名空间隔离。
    """
    stat_key = EVENT_STAT_KEY.get(event)
    if not stat_key:
        return
    if await _already_processed(db, user_id, event, ref_id, namespace="stats"):
        return
    await _mark_processed(db, user_id, event, ref_id, namespace="stats")
    await _bump_count(db, user_id, stat_key)
    await _recheck_achievements(db, user_id, stat_key)


async def apply_task_side_effects(
    db: AsyncSession,
    user_id: uuid.UUID,
    event: str,
    ref_id: str,
    *,
    today: str | None = None,
) -> None:
    """points-tasks 订阅入口：仅每日任务推进（独立幂等命名空间 tasks）。

    达标额外发分（``_advance_tasks`` 内 reward）归本订阅职责；reward 本身靠 ledger 唯一
    约束幂等，与本订阅的 ``tasks:`` 标记双重守约。
    """
    if EVENT_TASK_KEY.get(event) is None:
        return
    if await _already_processed(db, user_id, event, ref_id, namespace="tasks"):
        return
    await _mark_processed(db, user_id, event, ref_id, namespace="tasks")
    await _advance_tasks(db, user_id, event, today=today or _today())


async def _progress_for(db: AsyncSession, user_id: uuid.UUID, type_: str) -> int:
    """计算某成就类型的当前进度（读 UserBehaviorStat.stats）。"""
    stat = await _get_or_create_stats(db, user_id)
    key = {  # 成就 type → stats 键
        "post_count": "post",
        # 预留：本期无数据源，命中即返回 0（不额外建行为映射，YAGNI）
        "featured_count": "featured_count",
        "accepted_answers": "answer_accepted",
        "approved_files": "file_approved",
        "checkin_streak": "checkin_streak",
        "project_count": "project_count",
        "column_articles": "column_articles",
        "like_count": "like",
        "competition_count": "competition",
        "onboarding": "onboarding",
    }.get(type_)
    if not key:
        return 0
    return int(stat.stats.get(key, 0))


async def _recheck_achievements(
    db: AsyncSession, user_id: uuid.UUID, stat_key: str
) -> None:
    """对受影响的成就重算进度，达阈值即解锁。"""
    type_ = STAT_TO_ACHIEVEMENT_TYPE.get(stat_key)
    if type_ is None:
        return
    achievements = (
        (await db.execute(select(Achievement).where(Achievement.type == type_)))
        .scalars()
        .all()
    )
    progress = await _progress_for(db, user_id, type_)
    for ach in achievements:
        ua = (
            (
                await db.execute(
                    select(UserAchievement).where(
                        UserAchievement.user_id == user_id,
                        UserAchievement.achievement_id == ach.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if ua is None:
            # 并发下两个事务都可能查不到该行：UserAchievement 有
            # uq_user_achievement(user_id, achievement_id)，直接 insert+flush 撞唯一约束会
            # 抛未捕获 IntegrityError 拖垮整个事务（连已写的积分流水一起回滚）。改用
            # ON CONFLICT DO NOTHING 吸收撞键（同 _get_or_create_stats 的写法）后回读。
            await UserAchievementRepository(db).pg_upsert(
                {"user_id": user_id, "achievement_id": ach.id},
                constraint="uq_user_achievement",
                do_nothing=True,
            )
            ua = (
                (
                    await db.execute(
                        select(UserAchievement).where(
                            UserAchievement.user_id == user_id,
                            UserAchievement.achievement_id == ach.id,
                        )
                    )
                )
                .scalars()
                .one()
            )
        ua.progress = min(progress, ach.threshold)
        if not ua.unlocked and progress >= ach.threshold:
            ua.unlocked = True
            ua.unlocked_at = datetime.datetime.now(datetime.UTC)


async def _advance_tasks(
    db: AsyncSession, user_id: uuid.UUID, event: str, *, today: str
) -> None:
    """推进当日任务进度，达标且未奖励的发放额外积分。"""
    cat = EVENT_TASK_KEY.get(event)
    if cat is None:
        return
    tasks = (await db.execute(select(Task).where(Task.category == cat))).scalars().all()
    for t in tasks:
        up = (
            (
                await db.execute(
                    select(UserTaskProgress).where(
                        UserTaskProgress.user_id == user_id,
                        UserTaskProgress.task_id == t.id,
                        UserTaskProgress.period_date == today,
                    )
                )
            )
            .scalars()
            .first()
        )
        if up is None:
            # 同上（成就路径）：并发撞 uq_user_task_period 会抛未捕获 IntegrityError。
            # ON CONFLICT DO NOTHING 吸收后回读，再就地推进。
            await UserTaskProgressRepository(db).pg_upsert(
                {
                    "user_id": user_id,
                    "task_id": t.id,
                    "period_date": today,
                    "progress": 0,
                },
                constraint="uq_user_task_period",
                do_nothing=True,
            )
            up = (
                (
                    await db.execute(
                        select(UserTaskProgress).where(
                            UserTaskProgress.user_id == user_id,
                            UserTaskProgress.task_id == t.id,
                            UserTaskProgress.period_date == today,
                        )
                    )
                )
                .scalars()
                .one()
            )
        up.progress = min(int(up.progress) + 1, t.requirement_count)
        # 打卡特判：requirement_count==1 的 checkin 任务直接置 progress=1（幂等）
        if t.requirement_count == 1 and event == "checkin":
            up.progress = 1
        if not up.completed and up.progress >= t.requirement_count:
            up.completed = True
            # 达标额外发分（rewarded 防重复）
            if not up.rewarded:
                await reward(
                    db,
                    user_id,
                    t.reward_points,
                    "daily_task",
                    "task",
                    f"{t.key}:{today}",
                )
                up.rewarded = True
