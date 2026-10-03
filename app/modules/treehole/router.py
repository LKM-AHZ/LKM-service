"""Anonymous treehole API. A random, HttpOnly cookie owns private content."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Report as AdminReport
from app.modules.treehole.models import (
    Bottle,
    Conversation,
    Letter,
    Message,
    Reaction,
    Report,
    Wish,
    WishLight,
)
from app.modules.treehole.schemas import LetterInput, ReportInput, TextInput
from core.common import ApiResp
from core.db.base import now_iso
from core.db.session import get_session
from core.err import BizError, CommonErr, respond

router = APIRouter(prefix="/treehole", tags=["treehole"])
COOKIE = "lkm-treehole-session"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _owner(token: str | None) -> str | None:
    return (
        hashlib.sha256(token.encode()).hexdigest()
        if token and TOKEN_RE.fullmatch(token)
        else None
    )


def owner_optional(
    token: str | None = Cookie(default=None, alias=COOKIE),
) -> str | None:
    return _owner(token)


def owner_required(owner: str | None = Depends(owner_optional)) -> str:
    if owner is None:
        raise BizError(CommonErr.UNAUTHORIZED, "请先建立树洞匿名会话")
    return owner


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


@router.post("/session", response_model=ApiResp[dict])
async def session(
    request: Request, token: str | None = Cookie(default=None, alias=COOKIE)
) -> JSONResponse:
    token_value = (
        token if token is not None and _owner(token) else secrets.token_urlsafe(32)
    )
    response = JSONResponse({"code": 0, "message": "OK", "data": {"ready": True}})
    response.set_cookie(
        COOKIE,
        token_value,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        secure=(
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"
        ),
        samesite="lax",
        path="/api/v1/treehole",
    )
    return response


@router.get("/letters", response_model=ApiResp[list[dict]])
@respond
async def letters(
    scope: str = Query("public", pattern="^(public|mine|random)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=100),
    owner: str | None = Depends(owner_optional),
    db: AsyncSession = Depends(get_session),
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


@router.post("/letters", response_model=ApiResp[dict])
@respond
async def create_letter(
    body: LetterInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
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


@router.put("/letters/{letter_id}", response_model=ApiResp[dict])
@respond
async def edit_letter(
    letter_id: str,
    body: LetterInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
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


@router.delete("/letters/{letter_id}", response_model=ApiResp[dict])
@respond
async def remove_letter(
    letter_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
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


@router.post("/letters/{letter_id}/reactions/{kind}", response_model=ApiResp[dict])
@respond
async def toggle_reaction(
    letter_id: str,
    kind: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
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


@router.get("/bottles", response_model=ApiResp[list[dict]])
@respond
async def bottles(
    page: int = Query(1, ge=1),
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
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


@router.post("/bottles", response_model=ApiResp[dict])
@respond
async def create_bottle(
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
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


@router.post("/bottles/{bottle_id}/reply", response_model=ApiResp[dict])
@respond
async def reply_bottle(
    bottle_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
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


@router.get("/wishes", response_model=ApiResp[list[dict]])
@respond
async def wishes(
    page: int = Query(1, ge=1),
    owner: str | None = Depends(owner_optional),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
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


@router.post("/wishes", response_model=ApiResp[dict])
@respond
async def create_wish(
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
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


@router.put("/wishes/{wish_id}", response_model=ApiResp[dict])
@respond
async def edit_wish(
    wish_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    wish = await db.get(Wish, wish_id)
    if wish is None or wish.owner_id != owner:
        raise BizError(CommonErr.NOT_FOUND)
    if not body.text.strip():
        raise BizError(CommonErr.INVALID_INPUT)
    wish.text = body.text.strip()
    return {"updated": True}


@router.delete("/wishes/{wish_id}", response_model=ApiResp[dict])
@respond
async def remove_wish(
    wish_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    wish = await db.get(Wish, wish_id)
    if wish is None or wish.owner_id != owner:
        raise BizError(CommonErr.NOT_FOUND)
    await db.execute(delete(WishLight).where(WishLight.wish_id == wish_id))
    await db.delete(wish)
    return {"deleted": True}


@router.post("/wishes/{wish_id}/light", response_model=ApiResp[dict])
@respond
async def light_wish(
    wish_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
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


@router.post("/reports", response_model=ApiResp[dict])
@respond
async def report(
    body: ReportInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
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


@router.get("/conversations", response_model=ApiResp[list[dict]])
@respond
async def conversations(
    page: int = Query(1, ge=1),
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
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


@router.post("/letters/{letter_id}/reply", response_model=ApiResp[dict])
@respond
async def start_reply(
    letter_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
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


@router.post("/conversations/{conv_id}/messages", response_model=ApiResp[dict])
@respond
async def send_message(
    conv_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
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


@router.post(
    "/conversations/{conv_id}/messages/{message_id}/recall",
    response_model=ApiResp[dict],
)
@respond
async def recall(
    conv_id: str,
    message_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    await _conversation(db, conv_id, owner)
    msg = await db.get(Message, message_id)
    if msg is None or msg.conversation_id != conv_id or msg.sender_id != owner:
        raise BizError(CommonErr.NOT_FOUND)
    if now_iso() - msg.created_at > timedelta(minutes=2):
        raise BizError(CommonErr.FORBIDDEN, "超过两分钟无法撤回")
    msg.recalled = True
    msg.text = ""
    return {"recalled": True}


@router.post("/conversations/{conv_id}/block", response_model=ApiResp[dict])
@respond
async def block(
    conv_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    conv, side = await _conversation(db, conv_id, owner)
    setattr(conv, f"{side}_blocked", True)
    return {"blocked": True}


@router.post("/conversations/{conv_id}/clear", response_model=ApiResp[dict])
@respond
async def clear(
    conv_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    conv, side = await _conversation(db, conv_id, owner)
    setattr(conv, f"{side}_cleared_at", now_iso())
    return {"cleared": True}


@router.delete("/conversations/{conv_id}", response_model=ApiResp[dict])
@respond
async def hide_conversation(
    conv_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    conv, side = await _conversation(db, conv_id, owner)
    setattr(conv, f"{side}_hidden", True)
    setattr(conv, f"{side}_cleared_at", now_iso())
    return {"deleted": True}
