from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import respond
from app.db.session import get_read_session, get_session
from app.modules.starhope.schemas import (
    StarHopePullData,
    StarHopePushData,
    StarHopePushResult,
)
from app.modules.starhope.service import parse_since, pull_entity, push_entity
from auth.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/starhope", tags=["starhope"])


@router.get("/{entity}", response_model=ApiResp[StarHopePullData[dict]])
@respond
async def pull(
    entity: str,
    since: str | None = Query(default=None),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> StarHopePullData[dict]:
    return await pull_entity(db, entity, cur.id, parse_since(since))


@router.post("/{entity}/sync", response_model=ApiResp[StarHopePushResult])
@respond
async def push(
    entity: str,
    body: StarHopePushData,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> StarHopePushResult:
    return await push_entity(db, entity, cur.id, body.upserts, body.deletes)
