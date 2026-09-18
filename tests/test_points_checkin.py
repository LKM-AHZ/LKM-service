"""每日打卡（do_checkin）测试：奖励发放 / 幂等 / 打卡任务推进 / 打卡成就进度 / redis fail-open。"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.points.models import (
    Achievement,
    Task,
    UserAchievement,
    UserBehaviorStat,
    UserTaskProgress,
)
from app.modules.points.service import do_checkin, get_balance
from tests.conftest import auth_user_uid


async def _user(auth_db: AsyncSession, username: str = "checkin_user") -> uuid.UUID:
    """在 auth realm 建一线用户，返回其 uuid 主键（业务 points 表以裸 uuid 引用）。"""
    u = await auth_user_uid(auth_db, username=username, email=f"{username}@e.com")
    return u.id


async def _achievement(
    db: AsyncSession, key: str, type_: str, threshold: int
) -> uuid.UUID:
    a = Achievement(
        key=key,
        category="activity",
        icon="tabler:calendar-check",
        name_key=f"n_{key}",
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
async def test_first_checkin_earns_reward_and_streak(db: AsyncSession, auth_db: AsyncSession):
    """首次打卡：earned=5, streak=1，且写入 last_checkin_date。"""
    uid = await _user(auth_db)
    r = await do_checkin(db, uid)
    assert r["success"] is True
    assert r["earned"] == 5
    assert r["checkin_streak"] == 1
    assert r["today_checked"] is False
    stat = await db.get(UserBehaviorStat, uid)
    assert stat is not None
    assert stat.checkin_streak == 1
    assert stat.stats["checkin_streak"] == 1
    # 打卡本身发 RULE_DELTAS["checkin"]=5 分
    assert await get_balance(db, uid) == 5


@pytest.mark.asyncio
async def test_checkin_idempotent_same_day(db: AsyncSession, auth_db: AsyncSession):
    """同日再打：today_checked=True, earned=0，不重复发分。"""
    uid = await _user(auth_db)
    r1 = await do_checkin(db, uid)
    assert r1["earned"] == 5
    r2 = await do_checkin(db, uid)
    assert r2["earned"] == 0
    assert r2["today_checked"] is True
    assert await get_balance(db, uid) == 5  # 未重复发分


@pytest.mark.asyncio
async def test_checkin_advances_checkin_task(db: AsyncSession, auth_db: AsyncSession):
    """打卡推进 t1（checkin, req=1）任务：当日完成并发额外 5 分。"""
    uid = await _user(auth_db)
    await _task(db, "t1", "checkin", requirement_count=1, reward_points=5)
    await do_checkin(db, uid)
    up = (
        (
            await db.execute(
                select(UserTaskProgress).where(
                    UserTaskProgress.user_id == uid,
                    UserTaskProgress.period_date == UserBehaviorStat.last_checkin_date,
                )
            )
        )
        .scalars()
        .first()
    )
    assert up is not None
    assert up.completed and up.rewarded and up.progress == 1
    # 5(打卡) + 5(任务) = 10
    assert await get_balance(db, uid) == 10


@pytest.mark.asyncio
async def test_checkin_achievement_progress(db: AsyncSession, auth_db: AsyncSession):
    """成就 a9（checkin_streak 阈值 7）：首次打卡后 progress=1 但未解锁。"""
    uid = await _user(auth_db)
    await _achievement(db, "a9", "checkin_streak", threshold=7)
    await do_checkin(db, uid)
    ach = (
        (
            await db.execute(
                select(Achievement).where(Achievement.type == "checkin_streak")
            )
        )
        .scalars()
        .first()
    )
    assert ach is not None
    ua = (
        (
            await db.execute(
                select(UserAchievement).where(UserAchievement.achievement_id == ach.id)
            )
        )
        .scalars()
        .first()
    )
    assert ua is not None
    assert ua.progress == 1
    assert ua.unlocked is False  # streak 1 < 7


@pytest.mark.asyncio
async def test_checkin_fail_open_when_redis_unavailable(
    db: AsyncSession,
    auth_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Redis 操作时不可用（get/set/incr 抛错）→ 打卡的缓存读写仍 fail-open 不崩。"""
    uid = await _user(auth_db)

    class _DownRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, key, value, ex=None):
            raise RuntimeError("redis down")

        async def delete(self, *keys):
            raise RuntimeError("redis down")

        async def incr(self, key):
            raise RuntimeError("redis down")

    import app.core.cache as cache

    # redis_client.get_redis() 本身从不抛错（内部已兜底），但返回后的实际操作可能失败；
    # cache 各函数对 operation 都有 try/except → 抛错也应被吞掉，打卡不中断。
    async def _down_redis():
        return _DownRedis()

    monkeypatch.setattr(cache.redis_client, "get_redis", _down_redis)
    r = await do_checkin(db, uid)
    assert r["success"] is True
    assert r["earned"] == 5
    assert await get_balance(db, uid) == 5


@pytest.mark.asyncio
async def test_checkin_idempotent_streak_not_double_incremented(db: AsyncSession, auth_db: AsyncSession):
    """同日二次调用返回 0 且 streak 保持 1（幂等返回语义，防 streak 重入重复 +1）。"""
    uid = await _user(auth_db)
    r1 = await do_checkin(db, uid)
    assert r1["earned"] == 5 and r1["checkin_streak"] == 1
    # 第二次调用在同日已打 → 直接返回，不复读不改 streak
    r2 = await do_checkin(db, uid)
    assert r2["today_checked"] is True
    assert r2["earned"] == 0
    assert r2["checkin_streak"] == 1
    # 第三次仍幂等，streak 始终为 1
    r3 = await do_checkin(db, uid)
    assert r3["today_checked"] is True
    assert r3["checkin_streak"] == 1
    assert await get_balance(db, uid) == 5  # 只发了一次分
