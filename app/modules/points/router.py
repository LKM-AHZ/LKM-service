from math import ceil

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import (
    ApiResp,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
)
from app.core.err import respond
from app.db.session import get_read_session, get_session
from app.modules.points.schemas import (
    AchievementOut,
    BalanceOut,
    ExchangeItemOut,
    LeaderboardEntry,
    LedgerEntry,
    TaskOut,
)
from app.modules.points.service import (
    do_checkin,
    get_balance,
    leaderboard,
    list_achievements,
    list_exchange_items,
    list_ledger,
    list_tasks,
)
from auth.deps import CurrentUser, get_current_user


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
    return BalanceOut(user_id=cur.id, balance=await get_balance(db, cur.id))


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
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await do_checkin(db, cur.id)


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
        items=items, total=total, page=pag.page, pages=ceil(total / pag.limit)
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
