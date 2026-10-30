"""Treehole wishes operations; transaction ownership stays with the caller."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.treehole.models import (
    Wish,
    WishLight,
)
from app.modules.treehole.schemas import TextInput
from app.modules.treehole.services.common import (
    _ms,
    _rate_limit,
)
from core.db.base import now_iso
from core.err import BizError, CommonErr


async def wishes(page: int, owner: str | None, db: AsyncSession) -> list[dict]:
    rows = (
        await db.scalars(
            select(Wish)
            .order_by(Wish.created_at.desc())
            .offset((page - 1) * 100)
            .limit(100)
        )
    ).all()
    return [
        {
            "id": w.id,
            "text": w.text,
            "lights": w.lights,
            "createdAt": _ms(w.created_at),
            "ownerId": "me_local" if w.owner_id == owner else None,
        }
        for w in rows
    ]


async def create_wish(body: TextInput, owner: str, db: AsyncSession) -> dict:
    await _rate_limit(db, Wish, owner)
    wish = Wish(
        id=str(uuid.uuid4()),
        owner_id=owner,
        text=body.text.strip(),
        lights=0,
        created_at=now_iso(),
    )
    if not wish.text:
        raise BizError(CommonErr.INVALID_INPUT)
    db.add(wish)
    return {"id": wish.id}


async def edit_wish(
    wish_id: str, body: TextInput, owner: str, db: AsyncSession
) -> dict:
    wish = await db.get(Wish, wish_id)
    if wish is None or wish.owner_id != owner:
        raise BizError(CommonErr.NOT_FOUND)
    if not body.text.strip():
        raise BizError(CommonErr.INVALID_INPUT)
    wish.text = body.text.strip()
    return {"updated": True}


async def remove_wish(wish_id: str, owner: str, db: AsyncSession) -> dict:
    wish = await db.get(Wish, wish_id)
    if wish is None or wish.owner_id != owner:
        raise BizError(CommonErr.NOT_FOUND)
    await db.execute(delete(WishLight).where(WishLight.wish_id == wish_id))
    await db.delete(wish)
    return {"deleted": True}


async def light_wish(wish_id: str, owner: str, db: AsyncSession) -> dict:
    wish = await db.get(Wish, wish_id, with_for_update=True)
    if wish is None:
        raise BizError(CommonErr.NOT_FOUND)
    existing = await db.scalar(
        select(WishLight).where(
            WishLight.wish_id == wish_id, WishLight.owner_id == owner
        )
    )
    if existing is None:
        db.add(WishLight(id=str(uuid.uuid4()), wish_id=wish_id, owner_id=owner))
        wish.lights += 1
    return {"lights": wish.lights}
