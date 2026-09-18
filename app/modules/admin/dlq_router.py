"""Admin 端点：死信消息列表 / 重投 / 丢弃。"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import worker_dlq
from app.db.session import get_session
from app.modules.admin.deps import require_admin
from app.modules.admin.models import DlqMessage

router = APIRouter(prefix="/admin/dlq", tags=["admin-dlq"])


@router.get("")
async def list_dlq(
    status: str = "pending",
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict:
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
    return {
        "items": [
            {
                "id": m.id,
                "routing_key": m.routing_key,
                "status": m.status,
                "attempts": m.attempts,
                "reason": m.reason,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "payload": m.payload_json,
            }
            for m in rows
        ]
    }


@router.post("/{dlq_id}/requeue")
async def requeue_dlq(
    dlq_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict:
    ok = await worker_dlq.requeue(db, dlq_id)
    if not ok:
        raise HTTPException(status_code=400, detail="重投失败或非 pending")
    return {"ok": True}


@router.post("/{dlq_id}/discard")
async def discard_dlq(
    dlq_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _cur: Any = require_admin,
) -> dict:
    m = await db.scalar(select(DlqMessage).where(DlqMessage.id == dlq_id))
    if m is None:
        raise HTTPException(status_code=404, detail="not found")
    m.status = "discarded"
    await db.commit()
    return {"ok": True}
