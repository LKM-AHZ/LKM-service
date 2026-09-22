from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import BizError, CommonErr, respond
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
    since_dt = parse_since(since)
    if since is not None and since_dt is None:
        # parse_since 把无法解析的游标吞成 None → 时间过滤被丢掉，增量同步静默退化成
        # 全量拉取（还带上墓碑），客户端笔误却既无 4xx 也无日志。在边界显式拒绝。
        raise BizError(
            CommonErr.INVALID_INPUT, "since 必须是 ISO8601 时间字符串"
        )
    return await pull_entity(db, entity, cur.id, since_dt)


@router.post("/{entity}/sync", response_model=ApiResp[StarHopePushResult])
@respond
async def push(
    entity: str,
    body: StarHopePushData,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> StarHopePushResult:
    return await push_entity(db, entity, cur.id, body.upserts, body.deletes)
