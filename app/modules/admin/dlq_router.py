"""Admin 端点：死信消息列表 / 重投 / 丢弃。

响应统一走 ``@respond`` 包络（``{code, msg, data}``），与其余 admin 端点一致；
前端 ``readAdminResp`` 依赖该包络解析。
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import worker_dlq
from app.core.common import ApiResp, ListData
from app.core.err import BizError, CommonErr, respond
from app.db.session import get_session
from app.modules.admin.deps import require_admin
from app.modules.admin.models import DlqMessage

router = APIRouter(prefix="/admin/dlq", tags=["admin-dlq"])


class _DlqItem(BaseModel):
    """死信列表项（就地私有 schema，避免依赖业务 admin schemas）。"""

    id: uuid.UUID
    routing_key: str
    status: str
    attempts: int
    reason: str | None = None
    created_at: str | None = None
    payload: Any = None


@router.get("", response_model=ApiResp[ListData[_DlqItem]])
@respond
async def list_dlq(
    status: str = "pending",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> ListData[_DlqItem]:
    # id 是 UUIDPrimaryKeyMixin 的随机 UUID，无时间单调性，按它排序拿不到「最新死信」；
    # 且 DLQ 只增不减，必须带上限拉取（payload_json 是 JSONB 全量），否则单请求即爆内存。
    rows = (
        (
            await db.execute(
                select(DlqMessage)
                .where(DlqMessage.status == status)
                .order_by(DlqMessage.created_at.desc(), DlqMessage.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ListData(
        items=[
            _DlqItem(
                id=m.id,
                routing_key=m.routing_key,
                status=m.status,
                attempts=m.attempts,
                reason=m.reason,
                created_at=m.created_at.isoformat() if m.created_at else None,
                payload=m.payload_json,
            )
            for m in rows
        ]
    )


@router.post("/{dlq_id}/requeue", response_model=ApiResp[dict[str, Any]])
@respond
async def requeue_dlq(
    dlq_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict[str, Any]:
    ok = await worker_dlq.requeue(db, dlq_id)
    if not ok:
        # requeue 对「状态非法」与「下游发布失败」都返回 False（worker_dlq 的既有签名），
        # 但两者对调用方意义完全不同：把 MQ 故障当 400 参数错误报会给前端与监控双重误导。
        # 故先回滚（顺带释放 requeue 的 FOR UPDATE 行锁）再重读状态来区分。
        await db.rollback()
        m = await db.scalar(select(DlqMessage).where(DlqMessage.id == dlq_id))
        if m is None or m.status != "pending":
            raise BizError(CommonErr.INVALID_INPUT, "死信不存在或非 pending")
        raise BizError(CommonErr.UNAVAILABLE, "死信重投失败：下游消息总线不可用")
    return {"ok": True}


@router.post("/{dlq_id}/discard", response_model=ApiResp[dict[str, Any]])
@respond
async def discard_dlq(
    dlq_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict[str, Any]:
    # 行锁读 + 状态校验，与 worker_dlq.requeue 同一状态机：只允许 pending → discarded。
    # 不校验状态会让「已 requeued/已被处理」的记录被覆盖，并发 requeue/discard 也会互相覆盖。
    m = await db.scalar(
        select(DlqMessage).where(DlqMessage.id == dlq_id).with_for_update()
    )
    if m is None:
        raise BizError(CommonErr.INVALID_INPUT, "死信不存在")
    if m.status != "pending":
        raise BizError(CommonErr.INVALID_INPUT, "仅 pending 死信可丢弃")
    m.status = "discarded"
    await db.commit()
    return {"ok": True}
