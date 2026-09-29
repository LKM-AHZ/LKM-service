"""Admin 端点：relay 发布失败归档（``event_failures``）的列表 / 重放。

与 ``dlq_router`` 同形但**故障域不同**：DLQ（``dlq_messages``）管**消费侧**失败，本表管
**relay 发布侧**投不出的归档（见 ``app/db/event_failure.py`` 模块 docstring）。

蓝图 §5.1 第 3 条要求该表「驱动人工/脚本修复后重放」——此前只有只读分析入口
（ClickHouse ``lkm.event_failures`` 数据集），运维定位并修好根因后**没有可执行的重放手段**，
归档副本只能躺在表里。本路由补上这条通道。

响应统一走 ``@respond`` 包络（``{code, data, message, request_id}``），与其余 admin 端点一致。
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp, ListData
from app.core.err import BizError, CommonErr, respond
from app.db.event_failure import EventFailure, replay_failure
from app.db.session import get_session
from app.modules.admin.deps import require_admin

router = APIRouter(prefix="/admin/event-failures", tags=["admin-event-failures"])


class _EventFailureItem(BaseModel):
    """归档失败列表项（就地私有 schema，避免依赖业务 admin schemas）。"""

    id: uuid.UUID
    event_id: str
    routing_key: str
    attempt_count: int
    reason: str
    folded_at: str | None = None
    payload: Any = None


@router.get("", response_model=ApiResp[ListData[_EventFailureItem]])
@respond
async def list_event_failures(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> ListData[_EventFailureItem]:
    # 同 dlq_router：表只增不减、payload_json 是 JSONB 全量，必须带上限拉取，否则单请求
    # 即爆内存；按 folded_at 倒序取「最近归档的」供人工处置（id 是随机 uuid7/4，无排序意义）。
    rows = (
        (
            await db.execute(
                select(EventFailure)
                .order_by(EventFailure.folded_at.desc(), EventFailure.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ListData(
        items=[
            _EventFailureItem(
                id=r.id,
                event_id=r.event_id,
                routing_key=r.routing_key,
                attempt_count=r.attempt_count,
                reason=r.reason,
                folded_at=r.folded_at.isoformat() if r.folded_at else None,
                payload=r.payload_json,
            )
            for r in rows
        ]
    )


@router.post("/{failure_id}/replay", response_model=ApiResp[dict[str, Any]])
@respond
async def replay_event_failure(
    failure_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict[str, Any]:
    ok = await replay_failure(db, failure_id)
    if not ok:
        # ``replay_failure`` 对「归档行不存在」与「消息总线未启用」都返回 False，两者对
        # 调用方意义完全不同（前者是取错 id，后者是环境不可用）——回滚后重读来区分，
        # 与 dlq_router.requeue_dlq 同一处置思路。
        await db.rollback()
        row = await db.scalar(select(EventFailure).where(EventFailure.id == failure_id))
        if row is None:
            raise BizError(CommonErr.NOT_FOUND, "归档失败事件不存在或已被重放")
        raise BizError(CommonErr.UNAVAILABLE, "重放失败：消息总线未启用")
    return {"ok": True}
