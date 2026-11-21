"""日历 REST 出口。

响应刻意**不包** ``ApiResp`` 信封：桌面端 Solar SDK 直接读取裸 list/object 并依赖
``X-Total`` 头做分页，包装后客户端解析失败。写面仍走统一鉴权与错误码。
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.ports.authz import get_current_user, get_optional_user

from . import service
from .schemas import EventCreate, EventUpdate

router = APIRouter(prefix="/calendar", tags=["calendar"])

MAX_TAKE = 100


def _json(payload: object, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(payload), headers=headers)


def _page(rows: list[dict], total: int) -> JSONResponse:
    return _json(rows, headers={"X-Total": str(total)})


# ── 事件 CRUD ──────────────────────────────────────────────────────────
@router.get("/events")
async def list_events(
    start_time: datetime.datetime | None = Query(default=None, alias="startTime"),
    end_time: datetime.datetime | None = Query(default=None, alias="endTime"),
    account_id: uuid.UUID | None = Query(default=None, alias="accountId"),
    query: str | None = Query(default=None, max_length=200),
    tags: list[str] = Query(default=[]),
    offset: int = Query(default=0, ge=0),
    take: int = Query(default=50, ge=1, le=MAX_TAKE),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    if cur is None and account_id is None:
        return _page([], 0)
    rows, total = await service.list_events(
        db,
        viewer_id=cur.id if cur else None,
        owner_id=account_id,
        start_time=start_time,
        end_time=end_time,
        query=query,
        tags=tags,
        offset=offset,
        take=take,
    )
    return _page(rows, total)


@router.post("/events")
async def create_event(
    payload: EventCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    return _json(await service.create_event(db, cur.id, payload))


@router.get("/events/{event_id}")
async def get_event(
    event_id: uuid.UUID,
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    return _json(await service.get_event(db, event_id, cur.id if cur else None))


@router.put("/events/{event_id}")
async def update_event(
    event_id: uuid.UUID,
    payload: EventUpdate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    return _json(await service.update_event(db, event_id, cur.id, payload))


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    await service.delete_event(db, event_id, cur.id)
    return _json({})


# ── 月历 / 合并视图 / 倒计时 ───────────────────────────────────────────
@router.get("/month")
async def month(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    user_id: uuid.UUID | None = Query(default=None, alias="userId"),
    include_notable_days: bool = Query(default=True, alias="includeNotableDays"),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    entries = await service.month_entries(
        db,
        viewer_id=cur.id if cur else None,
        owner_id=user_id,
        year=year,
        month=month,
        include_notable_days=include_notable_days,
    )
    return _json(entries)


@router.get("/merged")
async def merged(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    user_id: uuid.UUID | None = Query(default=None, alias="userId"),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    return _json(
        await service.merged_view(
            db,
            viewer_id=cur.id if cur else None,
            owner_id=user_id,
            year=year,
            month=month,
        )
    )


@router.get("/countdown")
async def countdown(
    user_id: uuid.UUID | None = Query(default=None, alias="userId"),
    take: int = Query(default=20, ge=1, le=MAX_TAKE),
    offset: int = Query(default=0, ge=0),
    include_notable_days: bool = Query(default=True, alias="includeNotableDays"),
    tag: str | None = Query(default=None),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    if cur is None:
        return _page([], 0)
    rows, total = await service.countdown(
        db,
        viewer_id=cur.id,
        owner_id=user_id,
        include_notable_days=include_notable_days,
        tag=tag,
        offset=offset,
        take=take,
    )
    return _page(rows, total)


@router.get("/search")
async def search(
    query: str | None = Query(default=None, max_length=200),
    account_id: uuid.UUID | None = Query(default=None, alias="accountId"),
    tags: list[str] = Query(default=[]),
    start_time: datetime.datetime | None = Query(default=None, alias="startTime"),
    end_time: datetime.datetime | None = Query(default=None, alias="endTime"),
    notable_day_tag: int | None = Query(default=None, alias="notableDayTag"),
    offset: int = Query(default=0, ge=0),
    take: int = Query(default=50, ge=1, le=MAX_TAKE),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    del notable_day_tag  # 本服务没有节假日数据
    if cur is None and account_id is None:
        return _json([])
    rows = await service.search(
        db,
        viewer_id=cur.id if cur else None,
        query=query,
        account_id=account_id,
        tags=tags,
        start_time=start_time,
        end_time=end_time,
        offset=offset,
        take=take,
    )
    return _json(rows)


@router.get("/tags")
async def used_tags(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    return _json(await service.used_tags(db, cur.id))


# ── 订阅 ──────────────────────────────────────────────────────────────
@router.get("/subscriptions")
async def list_subscriptions(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    return _json(await service.list_subscriptions(db, cur.id))


@router.get("/subscriptions/subscribers")
async def list_subscribers(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> JSONResponse:
    return _json(await service.list_subscribers(db, cur.id))


@router.post("/subscriptions/{account_id}")
async def subscribe(
    account_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    await service.subscribe(db, cur.id, account_id)
    return _json({})


@router.delete("/subscriptions/{account_id}")
async def unsubscribe(
    account_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    await service.unsubscribe(db, cur.id, account_id)
    return _json({})
