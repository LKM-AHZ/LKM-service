"""Treehole conversations operations; transaction ownership stays with the caller."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.treehole.models import (
    Conversation,
    Letter,
    Message,
)
from app.modules.treehole.schemas import TextInput
from app.modules.treehole.services.common import (
    _get_letter,
    _ms,
    _visible,
)
from core.db.base import now_iso
from core.err import BizError, CommonErr


def _participant(conv: Conversation, owner: str) -> str:
    if conv.author_id == owner:
        return "author"
    if conv.replier_id == owner:
        return "replier"
    raise BizError(CommonErr.NOT_FOUND)


async def _conversation(
    db: AsyncSession, conv_id: str, owner: str
) -> tuple[Conversation, str]:
    conv = await db.get(Conversation, conv_id)
    if conv is None:
        raise BizError(CommonErr.NOT_FOUND)
    return conv, _participant(conv, owner)


async def conversations(page: int, owner: str, db: AsyncSession) -> list[dict]:
    rows = (
        await db.scalars(
            select(Conversation)
            .where(
                or_(
                    and_(
                        Conversation.author_id == owner,
                        Conversation.author_hidden.is_(False),
                    ),
                    and_(
                        Conversation.replier_id == owner,
                        Conversation.replier_hidden.is_(False),
                    ),
                )
            )
            .order_by(Conversation.updated_at.desc())
            .offset((page - 1) * 100)
            .limit(100)
        )
    ).all()
    result = []
    for conv in rows:
        side = _participant(conv, owner)
        cutoff = getattr(conv, f"{side}_cleared_at")
        query = select(Message).where(Message.conversation_id == conv.id)
        if cutoff:
            query = query.where(Message.created_at > cutoff)
        messages = list(
            (
                await db.scalars(query.order_by(Message.created_at.desc()).limit(200))
            ).all()
        )
        messages.reverse()
        letter = await db.get(Letter, conv.letter_id)
        result.append(
            {
                "id": conv.id,
                "myLetterId": conv.letter_id if side == "author" else "",
                "peerLetterId": conv.letter_id,
                "peerCodename": conv.replier_codename
                if side == "author"
                else (letter.codename if letter else "匿名"),
                "myCodename": (letter.codename if letter else "匿名")
                if side == "author"
                else conv.replier_codename,
                "blocked": conv.author_blocked or conv.replier_blocked,
                "updatedAt": _ms(conv.updated_at),
                "messages": [
                    {
                        "id": m.id,
                        "text": "已撤回" if m.recalled else m.text,
                        "recalled": m.recalled,
                        "from": "me" if m.sender_id == owner else "peer",
                        "at": _ms(m.created_at),
                    }
                    for m in messages
                ],
            }
        )
    return result


async def start_reply(
    letter_id: str, body: TextInput, owner: str, db: AsyncSession
) -> dict:
    letter = await _get_letter(db, letter_id)
    if (
        letter.owner_id == owner
        or letter.privacy not in {"public", "random"}
        or not _visible(letter, now_iso())
    ):
        raise BizError(CommonErr.NOT_FOUND)
    if not body.text.strip():
        raise BizError(CommonErr.INVALID_INPUT)
    conv = await db.scalar(
        select(Conversation).where(
            Conversation.letter_id == letter_id, Conversation.replier_id == owner
        )
    )
    if conv is None:
        conv = Conversation(
            id=str(uuid.uuid4()),
            letter_id=letter_id,
            author_id=letter.owner_id,
            replier_id=owner,
            replier_codename=f"访客{secrets.token_hex(3)}",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        db.add(conv)
    if conv.author_blocked or conv.replier_blocked:
        raise BizError(CommonErr.FORBIDDEN)
    now = now_iso()
    conv.updated_at = now
    conv.author_hidden = False
    conv.replier_hidden = False
    db.add(
        Message(
            id=str(uuid.uuid4()),
            conversation_id=conv.id,
            sender_id=owner,
            text=body.text.strip(),
            created_at=now,
        )
    )
    return {"conversationId": conv.id}


async def send_message(
    conv_id: str, body: TextInput, owner: str, db: AsyncSession
) -> dict:
    conv, _ = await _conversation(db, conv_id, owner)
    if conv.author_blocked or conv.replier_blocked:
        raise BizError(CommonErr.FORBIDDEN)
    if not body.text.strip():
        raise BizError(CommonErr.INVALID_INPUT)
    now = now_iso()
    conv.updated_at = now
    conv.author_hidden = False
    conv.replier_hidden = False
    msg = Message(
        id=str(uuid.uuid4()),
        conversation_id=conv_id,
        sender_id=owner,
        text=body.text.strip(),
        created_at=now,
    )
    db.add(msg)
    return {"id": msg.id}


async def recall(conv_id: str, message_id: str, owner: str, db: AsyncSession) -> dict:
    await _conversation(db, conv_id, owner)
    msg = await db.get(Message, message_id)
    if msg is None or msg.conversation_id != conv_id or msg.sender_id != owner:
        raise BizError(CommonErr.NOT_FOUND)
    if now_iso() - msg.created_at > timedelta(minutes=2):
        raise BizError(CommonErr.FORBIDDEN, "超过两分钟无法撤回")
    msg.recalled = True
    msg.text = ""
    return {"recalled": True}


async def block(conv_id: str, owner: str, db: AsyncSession) -> dict:
    conv, side = await _conversation(db, conv_id, owner)
    setattr(conv, f"{side}_blocked", True)
    return {"blocked": True}


async def clear(conv_id: str, owner: str, db: AsyncSession) -> dict:
    conv, side = await _conversation(db, conv_id, owner)
    setattr(conv, f"{side}_cleared_at", now_iso())
    return {"cleared": True}


async def hide_conversation(conv_id: str, owner: str, db: AsyncSession) -> dict:
    conv, side = await _conversation(db, conv_id, owner)
    setattr(conv, f"{side}_hidden", True)
    setattr(conv, f"{side}_cleared_at", now_iso())
    return {"deleted": True}
