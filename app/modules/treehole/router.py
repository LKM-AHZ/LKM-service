"""Treehole HTTP routes: validation, anonymous session and response envelopes."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Cookie, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.treehole.deps import (
    COOKIE,
    owner_from_token,
    owner_optional,
    owner_required,
)
from app.modules.treehole.schemas import LetterInput, ReportInput, TextInput
from app.modules.treehole.services import bottles as bottle_service
from app.modules.treehole.services import conversations as conversation_service
from app.modules.treehole.services import letters as letter_service
from app.modules.treehole.services import reports as report_service
from app.modules.treehole.services import wishes as wish_service
from core.common import ApiResp
from core.db.session import get_session
from core.err import respond

router = APIRouter(prefix="/treehole", tags=["treehole"])


@router.post("/session", response_model=ApiResp[dict])
async def session(
    request: Request, token: str | None = Cookie(default=None, alias=COOKIE)
) -> JSONResponse:
    token_value = (
        token
        if token is not None and owner_from_token(token)
        else secrets.token_urlsafe(32)
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
    return await letter_service.letters(
        scope=scope, page=page, limit=limit, owner=owner, db=db
    )


@router.post("/letters", response_model=ApiResp[dict])
@respond
async def create_letter(
    body: LetterInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await letter_service.create_letter(body=body, owner=owner, db=db)


@router.put("/letters/{letter_id}", response_model=ApiResp[dict])
@respond
async def edit_letter(
    letter_id: str,
    body: LetterInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await letter_service.edit_letter(
        letter_id=letter_id, body=body, owner=owner, db=db
    )


@router.delete("/letters/{letter_id}", response_model=ApiResp[dict])
@respond
async def remove_letter(
    letter_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await letter_service.remove_letter(letter_id=letter_id, owner=owner, db=db)


@router.post("/letters/{letter_id}/reactions/{kind}", response_model=ApiResp[dict])
@respond
async def toggle_reaction(
    letter_id: str,
    kind: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await letter_service.toggle_reaction(
        letter_id=letter_id, kind=kind, owner=owner, db=db
    )


@router.get("/bottles", response_model=ApiResp[list[dict]])
@respond
async def bottles(
    page: int = Query(1, ge=1),
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await bottle_service.bottles(page=page, owner=owner, db=db)


@router.post("/bottles", response_model=ApiResp[dict])
@respond
async def create_bottle(
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await bottle_service.create_bottle(body=body, owner=owner, db=db)


@router.post("/bottles/{bottle_id}/reply", response_model=ApiResp[dict])
@respond
async def reply_bottle(
    bottle_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await bottle_service.reply_bottle(
        bottle_id=bottle_id, body=body, owner=owner, db=db
    )


@router.get("/wishes", response_model=ApiResp[list[dict]])
@respond
async def wishes(
    page: int = Query(1, ge=1),
    owner: str | None = Depends(owner_optional),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await wish_service.wishes(page=page, owner=owner, db=db)


@router.post("/wishes", response_model=ApiResp[dict])
@respond
async def create_wish(
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await wish_service.create_wish(body=body, owner=owner, db=db)


@router.put("/wishes/{wish_id}", response_model=ApiResp[dict])
@respond
async def edit_wish(
    wish_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await wish_service.edit_wish(wish_id=wish_id, body=body, owner=owner, db=db)


@router.delete("/wishes/{wish_id}", response_model=ApiResp[dict])
@respond
async def remove_wish(
    wish_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await wish_service.remove_wish(wish_id=wish_id, owner=owner, db=db)


@router.post("/wishes/{wish_id}/light", response_model=ApiResp[dict])
@respond
async def light_wish(
    wish_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await wish_service.light_wish(wish_id=wish_id, owner=owner, db=db)


@router.post("/reports", response_model=ApiResp[dict])
@respond
async def report(
    body: ReportInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await report_service.report(body=body, owner=owner, db=db)


@router.get("/conversations", response_model=ApiResp[list[dict]])
@respond
async def conversations(
    page: int = Query(1, ge=1),
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await conversation_service.conversations(page=page, owner=owner, db=db)


@router.post("/letters/{letter_id}/reply", response_model=ApiResp[dict])
@respond
async def start_reply(
    letter_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await conversation_service.start_reply(
        letter_id=letter_id, body=body, owner=owner, db=db
    )


@router.post("/conversations/{conv_id}/messages", response_model=ApiResp[dict])
@respond
async def send_message(
    conv_id: str,
    body: TextInput,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await conversation_service.send_message(
        conv_id=conv_id, body=body, owner=owner, db=db
    )


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
    return await conversation_service.recall(
        conv_id=conv_id, message_id=message_id, owner=owner, db=db
    )


@router.post("/conversations/{conv_id}/block", response_model=ApiResp[dict])
@respond
async def block(
    conv_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await conversation_service.block(conv_id=conv_id, owner=owner, db=db)


@router.post("/conversations/{conv_id}/clear", response_model=ApiResp[dict])
@respond
async def clear(
    conv_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await conversation_service.clear(conv_id=conv_id, owner=owner, db=db)


@router.delete("/conversations/{conv_id}", response_model=ApiResp[dict])
@respond
async def hide_conversation(
    conv_id: str,
    owner: str = Depends(owner_required),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await conversation_service.hide_conversation(
        conv_id=conv_id, owner=owner, db=db
    )
