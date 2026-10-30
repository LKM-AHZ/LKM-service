"""Treehole bottles operations; transaction ownership stays with the caller."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.treehole.models import (
    Bottle,
)
from app.modules.treehole.schemas import TextInput
from app.modules.treehole.services.common import (
    _ms,
    _rate_limit,
)
from core.db.base import now_iso
from core.err import BizError, CommonErr


async def bottles(page: int, owner: str, db: AsyncSession) -> list[dict]:
    rows = (
        await db.scalars(
            select(Bottle)
            .where(or_(Bottle.picked_by.is_(None), Bottle.owner_id == owner))
            .order_by(Bottle.created_at.desc())
            .offset((page - 1) * 100)
            .limit(100)
        )
    ).all()
    return [
        {
            "id": b.id,
            "text": b.text,
            "createdAt": _ms(b.created_at),
            "ownerId": "me_local" if b.owner_id == owner else None,
            "picked": b.picked_by is not None,
            "reply": b.reply if b.owner_id == owner else None,
            "repliedAt": _ms(b.replied_at) if b.owner_id == owner else None,
        }
        for b in rows
    ]


async def create_bottle(body: TextInput, owner: str, db: AsyncSession) -> dict:
    await _rate_limit(db, Bottle, owner)
    bottle = Bottle(
        id=str(uuid.uuid4()),
        owner_id=owner,
        text=body.text.strip(),
        created_at=now_iso(),
    )
    if not bottle.text:
        raise BizError(CommonErr.INVALID_INPUT)
    db.add(bottle)
    return {"id": bottle.id}


async def reply_bottle(
    bottle_id: str, body: TextInput, owner: str, db: AsyncSession
) -> dict:
    bottle = await db.get(Bottle, bottle_id, with_for_update=True)
    if bottle is None or bottle.picked_by is not None or bottle.owner_id == owner:
        raise BizError(CommonErr.NOT_FOUND)
    if not body.text.strip():
        raise BizError(CommonErr.INVALID_INPUT)
    bottle.picked_by = owner
    bottle.reply = body.text.strip()
    bottle.replied_at = now_iso()
    return {"replied": True}
