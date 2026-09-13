"""points 三订阅扇出（M4）：声明同 topic 三订阅 + engine 三入口职责与命名空间隔离。

- 声明层（无需 DB）：reward/stats/tasks 三订阅指向同一 ``biz/points.apply`` topic、注册同一
  ``fn=apply_point_event``，但 handler 指向三个不同实现（扇出）。
- 行为层（需 PG）：stats 入口不推进任务、tasks 入口不碰行为计数；同名事件两入口各自执行一次、
  各自重跑幂等（``stats:``/``tasks:`` 命名空间互不误跳过）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import messaging, task_registry
from app.modules.points.engine import (
    apply_stats_side_effects,
    apply_task_side_effects,
)
from app.modules.points.models import Task, UserBehaviorStat, UserTaskProgress
from app.modules.points.tasks import (
    apply_point_daily_tasks,
    apply_point_reward,
    apply_point_stats,
)
from tests.conftest import auth_user_uid

_SUB_NAMES = (
    messaging.SUB_POINTS_REWARD.name,
    messaging.SUB_POINTS_STATS.name,
    messaging.SUB_POINTS_TASKS.name,
)


def test_three_subscriptions_same_topic_distinct_handlers() -> None:
    """三订阅同 topic、同 fn，但 handler 实现互异（扇出核心断言，无需 broker/DB）。"""
    task_registry.import_task_modules()
    assert {task_registry.subscription_topic(n) for n in _SUB_NAMES} == {
        messaging.TOPIC_POINTS
    }
    handlers = [task_registry.handlers_for(n)["apply_point_event"] for n in _SUB_NAMES]
    assert handlers[0] is apply_point_reward
    assert handlers[1] is apply_point_stats
    assert handlers[2] is apply_point_daily_tasks
    assert len(set(handlers)) == 3


async def _uid(auth_db: AsyncSession, username: str = "alice") -> int:
    u = await auth_user_uid(auth_db, username=username, email=f"{username}@e.com")
    return int(u.id)


async def _task(
    db: AsyncSession, key: str, category: str, requirement_count: int
) -> int:
    t = Task(
        key=key,
        title_key=f"t_{key}",
        desc_key=f"td_{key}",
        category=category,
        requirement_count=requirement_count,
        reward_points=0,
    )
    db.add(t)
    await db.flush()
    return t.id


async def test_stats_entry_does_not_advance_tasks(
    db: AsyncSession, auth_db: AsyncSession
) -> None:
    uid = await _uid(auth_db)
    await _task(db, "s_post", "post", requirement_count=1)
    await apply_stats_side_effects(db, uid, "post", "r1")
    await db.commit()

    stat = await db.get(UserBehaviorStat, uid)
    assert stat is not None and stat.stats["post"] == 1
    ups = (
        (
            await db.execute(
                select(UserTaskProgress).where(UserTaskProgress.user_id == uid)
            )
        )
        .scalars()
        .all()
    )
    assert ups == []  # stats 入口绝不推进任务


async def test_task_entry_does_not_bump_stats(
    db: AsyncSession, auth_db: AsyncSession
) -> None:
    uid = await _uid(auth_db)
    await _task(db, "t_post", "post", requirement_count=3)
    await apply_task_side_effects(db, uid, "post", "r1", today="2026-09-13")
    await db.commit()

    up = (
        (
            await db.execute(
                select(UserTaskProgress).where(UserTaskProgress.user_id == uid)
            )
        )
        .scalars()
        .one()
    )
    assert up.progress == 1
    stat = await db.get(UserBehaviorStat, uid)
    assert stat is not None and stat.stats.get("post", 0) == 0  # 未行为计数


async def test_namespaces_isolated_and_idempotent(
    db: AsyncSession, auth_db: AsyncSession
) -> None:
    """stats 重跑幂等；tasks 命名空间独立执行，不被 stats 的标记跳过。"""
    uid = await _uid(auth_db)
    await _task(db, "n_post", "post", requirement_count=5)

    await apply_stats_side_effects(db, uid, "post", "same")
    await apply_stats_side_effects(db, uid, "post", "same")  # 幂等：不 +2
    await apply_task_side_effects(db, uid, "post", "same", today="2026-09-13")
    await apply_task_side_effects(db, uid, "post", "same", today="2026-09-13")  # 幂等
    await db.commit()

    stat = await db.get(UserBehaviorStat, uid)
    assert stat is not None
    assert stat.stats["post"] == 1  # 行为计数只加一次
    processed = stat.stats["processed_events"]
    assert "stats:post:same" in processed
    assert "tasks:post:same" in processed

    up = (
        (
            await db.execute(
                select(UserTaskProgress).where(UserTaskProgress.user_id == uid)
            )
        )
        .scalars()
        .one()
    )
    assert up.progress == 1  # tasks 独立执行、只推进一次
