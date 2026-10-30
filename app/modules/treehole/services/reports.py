"""Treehole reports operations; transaction ownership stays with the caller."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Report as AdminReport
from app.modules.treehole.models import (
    Bottle,
    Letter,
    Report,
    Wish,
)
from app.modules.treehole.schemas import ReportInput
from core.db.base import now_iso
from core.err import BizError, CommonErr


async def report(body: ReportInput, owner: str, db: AsyncSession) -> dict:
    model = {"letter": Letter, "bottle": Bottle, "wish": Wish}[body.targetType]
    target = await db.get(model, body.targetId)
    if target is None:
        raise BizError(CommonErr.NOT_FOUND)
    existing = await db.scalar(
        select(Report).where(
            Report.reporter_id == owner,
            Report.target_type == body.targetType,
            Report.target_id == body.targetId,
        )
    )
    if existing is None:
        db.add(
            Report(
                id=str(uuid.uuid4()),
                reporter_id=owner,
                target_type=body.targetType,
                target_id=body.targetId,
                reason=body.reason,
                detail=body.detail,
                created_at=now_iso(),
            )
        )
        db.add(
            AdminReport(
                id=uuid.uuid4(),
                type=f"treehole_{body.targetType}",
                target_id=body.targetId,
                target_title=target.content[:200]
                if isinstance(target, Letter)
                else target.text[:200],
                reporter_id=None,
                reporter_name="树洞匿名访客",
                reason=f"{body.reason}: {body.detail}"[:500],
                status="pending",
                created_at=now_iso(),
            )
        )
    return {"reported": True}
