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

from app.modules.admin.deps import require_admin
from core import worker_dlq
from core.common import ApiResp, ListData
from core.config import settings
from core.db.dlq import DlqMessage
from core.db.session import get_session
from core.err import BizError, CommonErr, respond

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


class _RequeueBody(BaseModel):
    routing_key: str | None = None
    payload: dict[str, Any] | None = None


@router.get("", response_model=ApiResp[ListData[_DlqItem]])
@respond
async def list_dlq(
    status: str = "pending",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> ListData[_DlqItem]:
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
    body: _RequeueBody | None = None,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict[str, Any]:
    ok = await worker_dlq.requeue(
        db,
        dlq_id,
        routing_key=body.routing_key if body else None,
        payload=body.payload if body else None,
    )
    if not ok:
        await db.rollback()
        m = await db.scalar(select(DlqMessage).where(DlqMessage.id == dlq_id))
        if m is None or m.status != "pending":
            raise BizError(CommonErr.INVALID_INPUT, "死信不存在或非 pending")
        if not settings.message_bus_enabled:
            raise BizError(CommonErr.UNAVAILABLE, "死信重投失败：下游消息总线不可用")
        rk = m.routing_key if body is None or body.routing_key is None else body.routing_key
        payload = (m.payload_json or {}).get("payload") if body is None or body.payload is None else body.payload
        if not isinstance(payload, dict):
            raise BizError(CommonErr.INVALID_INPUT, "死信 payload 无效")
        reason = worker_dlq.messaging.permanent_failure_reason(rk, payload)
        if reason:
            raise BizError(CommonErr.INVALID_INPUT, f"死信重投事件无效：{reason}")
        raise BizError(CommonErr.CONFLICT, "死信重投状态已变化")
    return {"ok": True}


@router.post("/{dlq_id}/discard", response_model=ApiResp[dict[str, Any]])
@respond
async def discard_dlq(
    dlq_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict[str, Any]:
    # 只允许将 pending 死信标为 discarded。
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
