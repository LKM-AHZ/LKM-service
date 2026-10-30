"""Treehole letters operations; transaction ownership stays with the caller."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.treehole.models import (
    Conversation,
    Letter,
    Message,
    Reaction,
)
from app.modules.treehole.schemas import LetterInput
from app.modules.treehole.services.common import (
    _dt,
    _get_letter,
    _letter_data,
    _rate_limit,
    _visible,
)
from core.db.base import now_iso
from core.err import BizError, CommonErr


async def letters(
    scope: str, page: int, limit: int, owner: str | None, db: AsyncSession
) -> list[dict]:
    now = now_iso()
    query = select(Letter)
    if scope == "mine":
        if owner is None:
            raise BizError(CommonErr.UNAUTHORIZED)
        query = query.where(Letter.owner_id == owner)
    else:
        query = query.where(
            Letter.privacy == ("public" if scope == "public" else "random"),
            or_(Letter.scheduled_at.is_(None), Letter.scheduled_at <= now),
            or_(Letter.seal_until.is_(None), Letter.seal_until > now),
        )
    rows = (
        await db.scalars(
            query.order_by(Letter.created_at.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).all()
    return [await _letter_data(db, row, owner) for row in rows]


async def create_letter(body: LetterInput, owner: str, db: AsyncSession) -> dict:
    await _rate_limit(db, Letter, owner)
    now = now_iso()
    scheduled = _dt(body.scheduledAt)
    seal = _dt(body.sealUntil)
    if scheduled and scheduled <= now:
        raise BizError(CommonErr.INVALID_INPUT, "定时发布时间应晚于现在")
    if seal and seal <= (scheduled or now):
        raise BizError(CommonErr.INVALID_INPUT, "封存时间应晚于发布时间")
    letter = Letter(
        id=str(uuid.uuid4()),
        owner_id=owner,
        content=body.content.strip(),
        category=body.category,
        privacy=body.privacy,
        codename=body.codename,
        moods=body.moods,
        tags=body.tags,
        sticker=body.sticker,
        paper=body.paper,
        scheduled_at=scheduled,
        seal_until=seal,
        created_at=now,
        updated_at=now,
    )
    if not letter.content:
        raise BizError(CommonErr.INVALID_INPUT)
    db.add(letter)
    return await _letter_data(db, letter, owner)


async def edit_letter(
    letter_id: str, body: LetterInput, owner: str, db: AsyncSession
) -> dict:
    letter = await _get_letter(db, letter_id)
    if letter.owner_id != owner:
        raise BizError(CommonErr.FORBIDDEN)
    if not body.content.strip():
        raise BizError(CommonErr.INVALID_INPUT)
    for key in (
        "content",
        "category",
        "privacy",
        "codename",
        "moods",
        "tags",
        "sticker",
        "paper",
    ):
        setattr(letter, key, getattr(body, key))
    letter.content = body.content.strip()
    letter.scheduled_at = _dt(body.scheduledAt)
    letter.seal_until = _dt(body.sealUntil)
    if letter.seal_until and letter.seal_until <= (letter.scheduled_at or now_iso()):
        raise BizError(CommonErr.INVALID_INPUT)
    letter.updated_at = now_iso()
    return await _letter_data(db, letter, owner)


async def remove_letter(letter_id: str, owner: str, db: AsyncSession) -> dict:
    letter = await _get_letter(db, letter_id)
    if letter.owner_id != owner:
        raise BizError(CommonErr.FORBIDDEN)
    # Explicit deletion keeps SQLite tests and PostgreSQL migrations consistent.
    conv_ids = select(Conversation.id).where(Conversation.letter_id == letter_id)
    await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
    await db.execute(delete(Conversation).where(Conversation.letter_id == letter_id))
    await db.execute(delete(Reaction).where(Reaction.letter_id == letter_id))
    await db.delete(letter)
    return {"deleted": True}


async def toggle_reaction(
    letter_id: str, kind: str, owner: str, db: AsyncSession
) -> dict:
    if kind not in {"like", "favorite"}:
        raise BizError(CommonErr.INVALID_INPUT)
    letter = await _get_letter(db, letter_id)
    if letter.privacy != "public" or not _visible(letter, now_iso()):
        raise BizError(CommonErr.NOT_FOUND)
    existing = await db.scalar(
        select(Reaction).where(
            Reaction.letter_id == letter_id,
            Reaction.owner_id == owner,
            Reaction.kind == kind,
        )
    )
    if existing:
        await db.delete(existing)
        active = False
    else:
        db.add(
            Reaction(
                id=str(uuid.uuid4()), letter_id=letter_id, owner_id=owner, kind=kind
            )
        )
        active = True
    await db.flush()
    return {"active": active, "letter": await _letter_data(db, letter, owner)}
