"""Shared treehole visibility, serialization and per-owner write limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.treehole.models import (
    Bottle,
    Letter,
    Reaction,
    Wish,
)
from core.db.base import now_iso
from core.err import BizError, CommonErr


def _ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def _dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OverflowError):
        raise BizError(CommonErr.INVALID_INPUT, "时间无效") from None


def _visible(letter: Letter, now: datetime) -> bool:
    return (letter.scheduled_at is None or letter.scheduled_at <= now) and (
        letter.seal_until is None or letter.seal_until > now
    )


async def _letter_data(db: AsyncSession, letter: Letter, owner: str | None) -> dict:
    counts = dict(
        (
            await db.execute(
                select(Reaction.kind, func.count(Reaction.id))
                .where(Reaction.letter_id == letter.id)
                .group_by(Reaction.kind)
            )
        ).all()
    )
    mine = set()
    if owner:
        mine = set(
            (
                await db.scalars(
                    select(Reaction.kind).where(
                        Reaction.letter_id == letter.id, Reaction.owner_id == owner
                    )
                )
            ).all()
        )
    now = now_iso()
    visible = _visible(letter, now)
    return {
        "id": letter.id,
        "content": letter.content,
        "category": letter.category,
        "privacy": letter.privacy,
        "codename": letter.codename,
        "moods": letter.moods,
        "tags": letter.tags,
        "sticker": letter.sticker,
        "paper": letter.paper,
        "status": "sealed"
        if letter.seal_until and letter.seal_until <= now
        else "scheduled"
        if not visible
        else "published",
        "scheduledAt": _ms(letter.scheduled_at),
        "sealUntil": _ms(letter.seal_until),
        "createdAt": _ms(letter.created_at),
        "updatedAt": _ms(letter.updated_at),
        "publishedAt": _ms(letter.scheduled_at or letter.created_at)
        if visible
        else None,
        "likes": counts.get("like", 0),
        "favorites": counts.get("favorite", 0),
        "liked": "like" in mine,
        "favorited": "favorite" in mine,
        "isMine": owner == letter.owner_id,
    }


async def _get_letter(db: AsyncSession, letter_id: str) -> Letter:
    letter = await db.get(Letter, letter_id)
    if letter is None:
        raise BizError(CommonErr.NOT_FOUND)
    return letter


async def _rate_limit(
    db: AsyncSession,
    model: type[Letter] | type[Bottle] | type[Wish],
    owner: str,
    limit: int = 3,
) -> None:
    count = await db.scalar(
        select(func.count())
        .select_from(model)
        .where(
            model.owner_id == owner, model.created_at > now_iso() - timedelta(minutes=1)
        )
    )
    if count is not None and count >= limit:
        raise BizError(CommonErr.BAD_REQUEST, "操作太频繁，请稍后重试")
