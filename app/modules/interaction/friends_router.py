"""desktop 好友界面的 Stargate 兼容 REST 面（``/stargate/*``）。

desktop 直接把这些响应解析成 ``SnRelationship`` / ``SnFriendOverviewItem``，故这里
**不走统一 ApiResp 信封**：列表返回裸数组，分页总数放 ``X-Total`` 响应头。业务实现见
:mod:`app.modules.interaction.friends_service`。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.interaction import friends_service
from app.modules.interaction.schemas import (
    RelationshipAliasUpdate,
    RelationshipBlockRequest,
    RelationshipMuteRequest,
    RelationshipStatusUpdate,
    StargateFriendOverviewOut,
    StargateRelationshipOut,
)
from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.ports.authz import get_current_user

router = APIRouter(prefix="/stargate", tags=["stargate-friends"])


def _base_url(request: Request) -> str:
    """供头像绝对 URL 使用；网关透传 X-Forwarded-*，uvicorn 已按代理头还原。"""
    return str(request.base_url)


@router.get(
    "/relationships/requests", response_model=list[StargateRelationshipOut]
)
async def list_friend_requests(
    request: Request,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[StargateRelationshipOut]:
    views = await friends_service.list_requests(db, cur.id)
    return await friends_service.build_relationships(
        db, views, base_url=_base_url(request)
    )


@router.get("/relationships", response_model=list[StargateRelationshipOut])
async def list_relationships(
    request: Request,
    response: Response,
    offset: int = Query(default=0, ge=0),
    take: int = Query(default=20, ge=1, le=100),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[StargateRelationshipOut]:
    views, total = await friends_service.list_relationships(
        db, cur.id, offset=offset, take=take
    )
    response.headers["X-Total"] = str(total)
    return await friends_service.build_relationships(
        db, views, base_url=_base_url(request)
    )


@router.get("/friends/overview", response_model=list[StargateFriendOverviewOut])
async def friends_overview(
    request: Request,
    include_offline: bool = Query(default=False, alias="includeOffline"),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[StargateFriendOverviewOut]:
    return await friends_service.build_friend_overview(
        db,
        cur.id,
        base_url=_base_url(request),
        include_offline=include_offline,
    )


@router.post("/relationships/{target_id}/friends")
async def send_friend_request(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.send_request(db, cur.id, target_id)
    return {"ok": True}


@router.post("/relationships/{target_id}/friends/accept")
async def accept_friend_request(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.accept_request(db, cur.id, target_id)
    return {"ok": True}


@router.post("/relationships/{target_id}/friends/decline")
async def decline_friend_request(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.decline_request(db, cur.id, target_id)
    return {"ok": True}


@router.delete("/relationships/{target_id}/friends")
async def cancel_friend_request(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.cancel_request(db, cur.id, target_id)
    return {"ok": True}


@router.patch("/relationships/{target_id}")
async def update_relationship(
    target_id: uuid.UUID,
    payload: RelationshipStatusUpdate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.update_status(db, cur.id, target_id, payload.status)
    return {"ok": True}


@router.delete("/relationships/{target_id}")
async def delete_relationship(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.delete_relationship(db, cur.id, target_id)
    return {"ok": True}


@router.post("/relationships/{target_id}/block")
async def block_user(
    target_id: uuid.UUID,
    payload: RelationshipBlockRequest | None = Body(default=None),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.block(
        db,
        cur.id,
        target_id,
        payload.expires_in if payload else None,
        payload.degrade_to if payload else None,
    )
    return {"ok": True}


@router.post("/relationships/{target_id}/mute")
async def mute_user(
    target_id: uuid.UUID,
    payload: RelationshipMuteRequest | None = Body(default=None),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.mute(
        db, cur.id, target_id, payload.expires_in if payload else None
    )
    return {"ok": True}


@router.post("/relationships/{target_id}/unmute")
async def unmute_user(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.unmute(db, cur.id, target_id)
    return {"ok": True}


@router.post("/relationships/{target_id}/close-friend")
async def add_close_friend(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.add_close_friend(db, cur.id, target_id)
    return {"ok": True}


@router.delete("/relationships/{target_id}/close-friend")
async def remove_close_friend(
    target_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.remove_close_friend(db, cur.id, target_id)
    return {"ok": True}


@router.patch("/relationships/{target_id}/alias")
async def update_relationship_alias(
    target_id: uuid.UUID,
    payload: RelationshipAliasUpdate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await friends_service.update_alias(db, cur.id, target_id, payload.alias)
    return {"ok": True}
