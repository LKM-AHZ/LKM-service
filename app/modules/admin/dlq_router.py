"""Admin 端点：死信消息列表 / 重投 / 丢弃。

响应统一走 ``@respond`` 包络（``{code, msg, data}``），与其余 admin 端点一致；
前端 ``readAdminResp`` 依赖该包络解析。
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
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
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> ListData[_DlqItem]:
    rows = (
        (
            await db.execute(
                select(DlqMessage)
                .where(DlqMessage.status == status)
                .order_by(DlqMessage.id.desc())
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
        raise BizError(CommonErr.INVALID_INPUT, "重投失败或非 pending")
    return {"ok": True}


@router.post("/{dlq_id}/discard", response_model=ApiResp[dict[str, Any]])
@respond
async def discard_dlq(
    dlq_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict[str, Any]:
    m = await db.scalar(select(DlqMessage).where(DlqMessage.id == dlq_id))
    if m is None:
        raise BizError(CommonErr.INVALID_INPUT, "死信不存在")
    m.status = "discarded"
    await db.commit()
    return {"ok": True}
