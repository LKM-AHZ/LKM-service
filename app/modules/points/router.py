import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.points.schemas import (
    AchievementOut,
    BalanceOut,
    CheckInHistoryItem,
    CheckInResultOut,
    ExchangeItemOut,
    LeaderboardEntry,
    LedgerEntry,
    TaskOut,
)
from app.modules.points.service import (
    do_checkin,
    get_balance,
    get_today_checkin,
    leaderboard,
    level_for_balance,
    list_achievements,
    list_checkin_history,
    list_exchange_items,
    list_ledger,
    list_tasks,
)
from core.common import (
    ApiResp,
    ListData,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
    paginate_pages,
)
from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.err import respond
from core.ports.authz import get_current_user


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="points",
        status="implemented",
        responsibility="积分核心引擎：balance+ledger 账本，reward/spend/transfer 原子原语，排行榜。",
        next_steps=[
            "QA 悬赏转账回接（阶段5）",
            "竞赛积分回接（reward 接口）",
            "事件自动挂接规则（发帖/评论/加精加分）",
        ],
    )


router = APIRouter(prefix="/points", tags=["points"])


@router.get("/status", response_model=ModuleStatus)
async def points_status() -> ModuleStatus:
    return _status()


@router.get("/me", response_model=ApiResp[BalanceOut])
@respond
async def my_balance(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> BalanceOut:
    balance = await get_balance(db, cur.id)
    level, next_level_points, level_progress = level_for_balance(balance)
    return BalanceOut(
        user_id=cur.id,
        balance=balance,
        level=level,
        next_level_points=next_level_points,
        level_progress=level_progress,
    )


@router.get("/me/ledger", response_model=ApiResp[PageData[LedgerEntry]])
@respond
async def my_ledger(
    cur: CurrentUser = Depends(get_current_user),
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[LedgerEntry]:
    return await list_ledger(db, cur.id, page=pag.page, limit=pag.limit)


@router.post("/checkin", response_model=ApiResp[dict])
@respond
async def checkin(
    date: datetime.date | None = Query(
        None, description="补签日期（YYYY-MM-DD）；为空表示今天"
    ),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await do_checkin(db, cur.id, target_date=date)


@router.get("/checkin/today", response_model=ApiResp[CheckInResultOut | None])
@respond
async def today_checkin(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> CheckInResultOut | None:
    return await get_today_checkin(db, cur.id)


@router.get(
    "/checkin/history", response_model=ApiResp[ListData[CheckInHistoryItem]]
)
@respond
async def checkin_history(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> dict:
    items = await list_checkin_history(
        db, cur.id, year=year, month=month
    )
    return {"items": items}


@router.get("/leaderboard", response_model=ApiResp[PageData[LeaderboardEntry]])
@respond
async def points_leaderboard(
    pag: PaginateParams = Depends(PaginateDep()),
    period: str = Query("total"),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[LeaderboardEntry]:
    items, total = await leaderboard(
        db, offset=pag.offset, limit=pag.limit, period=period
    )
    return PageData(
        items=items, total=total, page=pag.page, pages=paginate_pages(total, pag.limit)
    )


@router.get("/achievements", response_model=ApiResp[list[AchievementOut]])
@respond
async def points_achievements(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[AchievementOut]:
    return await list_achievements(db, user_id=cur.id)


@router.get("/tasks", response_model=ApiResp[list[TaskOut]])
@respond
async def points_tasks(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> list[TaskOut]:
    return await list_tasks(db, user_id=cur.id)


@router.get("/exchange-items", response_model=ApiResp[list[ExchangeItemOut]])
@respond
async def points_exchange_items(
    db: AsyncSession = Depends(get_read_session),
) -> list[ExchangeItemOut]:
    return await list_exchange_items(db)
