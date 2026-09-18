"""积分事件副作用测试：行为计数 / 成就解锁 / 任务推进与达标发分。"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.points.engine import (
    EVENT_STAT_KEY,
    STAT_TO_ACHIEVEMENT_TYPE,
    apply_event_side_effects,
)
from app.modules.points.models import (
    Achievement,
    Task,
    UserAchievement,
    UserBehaviorStat,
    UserTaskProgress,
)
from app.modules.points.service import get_balance
from tests.conftest import auth_user_uid


async def _user(auth_db: AsyncSession, username: str = "alice") -> uuid.UUID:
    """在 auth realm 建一线用户，返回其 uuid 主键（业务 points 表以裸 uuid 引用）。"""
    u = await auth_user_uid(
        auth_db, username=username, email=f"{username}@e.com"
    )
    return u.id


async def _achievement(
    db: AsyncSession, key: str, type_: str, threshold: int
) -> uuid.UUID:
    a = Achievement(
        key=key,
        category="special",
        icon="tabler:star",
        name_key=f"a_{key}",
        desc_key=f"d_{key}",
        type=type_,
        threshold=threshold,
        reward_points=0,
    )
    db.add(a)
    await db.flush()
    return a.id


async def _task(
    db: AsyncSession,
    key: str,
    category: str,
    requirement_count: int,
    reward_points: int,
) -> uuid.UUID:
    t = Task(
        key=key,
        title_key=f"t_{key}",
        desc_key=f"td_{key}",
        category=category,
        requirement_count=requirement_count,
        reward_points=reward_points,
    )
    db.add(t)
    await db.flush()
    return t.id


@pytest.mark.asyncio
async def test_event_bumps_count_and_achievement(db: AsyncSession, auth_db: AsyncSession):
    uid = await _user(auth_db)
    await _achievement(db, "a1", "post_count", threshold=1)
    await apply_event_side_effects(db, uid, "post", "p100", today="2026-08-21")
    stat = await db.get(UserBehaviorStat, uid)
    assert stat is not None and stat.stats["post"] == 1
    # 成就达阈值 → 解锁
    ach = (
        (await db.execute(select(Achievement).where(Achievement.type == "post_count")))
        .scalars()
        .first()
    )
    assert ach is not None
    ua = (
        (
            await db.execute(
                select(UserAchievement).where(
                    UserAchievement.user_id == uid,
                    UserAchievement.achievement_id == ach.id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert ua is not None and ua.unlocked and ua.progress == 1


@pytest.mark.asyncio
async def test_achieve_requires_threshold(db: AsyncSession, auth_db: AsyncSession):
    """计数未达阈值时成就保持未解锁。"""
    uid = await _user(auth_db)
    await _achievement(db, "a2", "like_count", threshold=5)
    await apply_event_side_effects(db, uid, "like", "l1", today="2026-08-21")
    ach = (
        (await db.execute(select(Achievement).where(Achievement.type == "like_count")))
        .scalars()
        .first()
    )
    assert ach is not None
    ua = (
        (
            await db.execute(
                select(UserAchievement).where(
                    UserAchievement.user_id == uid,
                    UserAchievement.achievement_id == ach.id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert ua is not None and not ua.unlocked
    assert ua.progress == 1


@pytest.mark.asyncio
async def test_task_progress_and_reward(db: AsyncSession, auth_db: AsyncSession):
    """发帖推进当日任务，达标后额外发奖励分且只发一次。"""
    uid = await _user(auth_db)
    await _task(db, "t1", "post", requirement_count=2, reward_points=30)
    await apply_event_side_effects(db, uid, "post", "p1", today="2026-08-21")
    await apply_event_side_effects(db, uid, "post", "p2", today="2026-08-21")
    up = (
        (
            await db.execute(
                select(UserTaskProgress).where(
                    UserTaskProgress.user_id == uid,
                    UserTaskProgress.period_date == "2026-08-21",
                )
            )
        )
        .scalars()
        .first()
    )
    assert up is not None and up.completed and up.rewarded and up.progress == 2
    # 达标额外发 30 分（幂等只发一次）
    assert await get_balance(db, uid) == 30


@pytest.mark.asyncio
async def test_task_reward_not_duplicated(db: AsyncSession, auth_db: AsyncSession):
    """同一天多次达标后不再重复发分。"""
    uid = await _user(auth_db)
    await _task(db, "t2", "post", requirement_count=1, reward_points=10)
    for ref in ("p1", "p2", "p3"):
        await apply_event_side_effects(db, uid, "post", ref, today="2026-08-21")
    assert await get_balance(db, uid) == 10


@pytest.mark.asyncio
async def test_checkin_task_force_progress(db: AsyncSession, auth_db: AsyncSession):
    """打卡事件特判：requirement_count==1 的 checkin 任务置 progress=1 并解锁。"""
    uid = await _user(auth_db)
    await _task(db, "t3", "checkin", requirement_count=1, reward_points=5)
    await apply_event_side_effects(db, uid, "checkin", "c1", today="2026-08-21")
    up = (
        (
            await db.execute(
                select(UserTaskProgress).where(
                    UserTaskProgress.user_id == uid,
                    UserTaskProgress.period_date == "2026-08-21",
                )
            )
        )
        .scalars()
        .first()
    )
    assert up is not None and up.completed and up.rewarded and up.progress == 1
    assert await get_balance(db, uid) == 5


@pytest.mark.asyncio
async def test_event_stat_key_mapping(db: AsyncSession, auth_db: AsyncSession):
    """事件→计数键 与 计数键→成就 type 映射为规划值（防回归）。"""
    assert EVENT_STAT_KEY["post"] == "post"
    assert EVENT_STAT_KEY["comment"] == "comment"
    assert EVENT_STAT_KEY["file_approved"] == "file_approved"
    assert STAT_TO_ACHIEVEMENT_TYPE["post"] == "post_count"
    assert STAT_TO_ACHIEVEMENT_TYPE["like"] == "like_count"


@pytest.mark.asyncio
async def test_unknown_event_is_noop(db: AsyncSession, auth_db: AsyncSession):
    """未知/未映射事件不抛错、不计数、不建统计行。"""
    uid = await _user(auth_db)
    await apply_event_side_effects(db, uid, "unknown_event", "x", today="2026-08-21")
    stat = await db.get(UserBehaviorStat, uid)
    assert stat is None


@pytest.mark.asyncio
async def test_apply_side_effects_is_idempotent_per_ref(db: AsyncSession, auth_db: AsyncSession):
    """副作用以 (event, ref_id) 去重：重复投递不重复累计计数/进度/发分(重投语义)。"""
    uid = await _user(auth_db)
    await _achievement(db, "a2", "post_count", threshold=1)
    await _task(db, "t5", "post", requirement_count=2, reward_points=5)

    # 同一 ref 投递两次（模拟 commit 成功但 ack 前崩溃后重投）
    for _ in range(2):
        await apply_event_side_effects(db, uid, "post", "ref-1", today="2026-08-21")

    stat = await db.get(UserBehaviorStat, uid)
    assert stat is not None
    assert stat.stats["post"] == 1  # 只累计一次
    ua = (
        (
            await db.execute(
                select(UserAchievement).where(UserAchievement.user_id == uid)
            )
        )
        .scalars()
        .first()
    )
    assert ua is not None and ua.progress == 1

    # 不同 ref 正常累计
    await apply_event_side_effects(db, uid, "post", "ref-2", today="2026-08-21")
    stat = await db.get(UserBehaviorStat, uid)
    assert stat.stats["post"] == 2
