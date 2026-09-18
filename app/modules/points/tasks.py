"""points 模块订阅任务：积分事件扇出为 reward / stats / tasks 三订阅（M4）。

Pulsar 同一 topic ``biz/points.apply`` 挂三个 Shared subscription，各收全量事件、各司其职：

- ``points-reward``：仅 ``reward()`` 入账（ledger ref 唯一约束保底幂等）。
- ``points-stats``：行为计数 + 成就重算（``engine.apply_stats_side_effects``，stats 命名空间）。
- ``points-tasks``：每日任务推进（``engine.apply_task_side_effects``，tasks 命名空间）。

三者各由独立 worker 进程消费、独立事务，单订阅故障（重投/死信）不影响其余（故障隔离）。
消息 ``fn`` 均为 ``apply_point_event``，各订阅 handler 表指向不同实现。worker 无请求上下文，
用 app.db.session.new_session() 自建会话。
"""

import uuid

from app.core.messaging import (
    SUB_POINTS_REWARD,
    SUB_POINTS_STATS,
    SUB_POINTS_TASKS,
)
from app.core.task_registry import register_task
from app.db.session import new_session
from app.modules.points.rules import RULE_DELTAS
from app.modules.points.service import reward


async def apply_point_reward(user_id: uuid.UUID, event: str, ref_id: str) -> None:
    """points-reward 订阅：积分入账（幂等靠 ledger ref 唯一约束）。"""
    db = await new_session()
    try:
        delta = RULE_DELTAS.get(event, 0)
        # answer_accepted 或未知事件不额外发分（QA 已派发 bounty）
        if delta > 0:
            await reward(db, user_id, delta, event, event, ref_id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def apply_point_stats(user_id: uuid.UUID, event: str, ref_id: str) -> None:
    """points-stats 订阅：行为计数 + 成就重算。"""
    from app.modules.points.engine import apply_stats_side_effects

    db = await new_session()
    try:
        await apply_stats_side_effects(db, user_id, event, ref_id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def apply_point_daily_tasks(user_id: uuid.UUID, event: str, ref_id: str) -> None:
    """points-tasks 订阅：每日任务推进 + 达标奖励。"""
    from app.modules.points.engine import apply_task_side_effects

    db = await new_session()
    try:
        await apply_task_side_effects(db, user_id, event, ref_id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


register_task(SUB_POINTS_REWARD.name, "apply_point_event", apply_point_reward)
register_task(SUB_POINTS_STATS.name, "apply_point_event", apply_point_stats)
register_task(SUB_POINTS_TASKS.name, "apply_point_event", apply_point_daily_tasks)
