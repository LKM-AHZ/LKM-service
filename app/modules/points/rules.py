"""积分规则表：事件类型 → 单次积分。事件属展示/计数 + 入账两用。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import RKEY_POINTS
from app.db.outbox import enqueue_outbox

# event → delta 奖励分（answer_accepted 不给分：QA 已按悬赏派发，见设计中说明）
RULE_DELTAS: dict[str, int] = {
    "post": 10,
    "comment": 2,
    "like": 1,
    "file_approved": 15,
    "answer_accepted": 0,  # 只计数不加分（QA 已派发 bounty）
    "checkin": 5,
    "competition": 50,
}


async def enqueue_points_event(
    db: AsyncSession, user_id: uuid.UUID, event: str, ref_id: str
) -> None:
    """把用户行为事件排进 outbox（与业务同事务落库，relay 会投给 points worker 入账）。

    未配置消息总线 → outbox 门控直返（不落积压），维持 dev 下 fire-and-forget 的无害性；
    配置生效后该事件关联业务自身 commit 一并持久，达「DB 成、事件必达」。
    """
    await enqueue_outbox(
        db, RKEY_POINTS, {"fn": "apply_point_event", "args": [user_id, event, ref_id]}
    )
