"""interaction REST：收藏/取消/我的收藏 + 浏览上报/我的浏览历史。

写操作走权限点（interaction.favorite / interaction.history），读自己的列表只要求登录。
"""

import uuid

from fastapi import APIRouter, Depends
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
from app.modules.interaction.schemas import (
    FavoriteItem,
    FavoriteState,
    HistoryItem,
    ViewState,
)
from app.modules.interaction.service import (
    add_favorite,
    list_favorites,
    list_history,
    record_view,
    remove_favorite,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/interaction", tags=["interaction"])


@router.get("/status", response_model=ModuleStatus)
async def interaction_status() -> ModuleStatus:
    return ModuleStatus(
        module="interaction",
        status="implemented",
        responsibility="用户互动：内容收藏（幂等）+ 浏览记录（upsert 幂等，带保留期）。",
        next_steps=[
            "浏览明细改走 ClickHouse（若 PG 写放大可见，需先在路线图 §8 登记分叉）",
            "互动计数 Redis 链路 + 对账（M6.10，判据式）",
        ],
    )


@router.post("/favorites/{content_id}", response_model=ApiResp[FavoriteState])
@respond
async def favorite(
    content_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.interaction_favorite),
    db: AsyncSession = Depends(get_session),
) -> FavoriteState:
    return await add_favorite(db, cur.id, content_id)


@router.delete("/favorites/{content_id}", response_model=ApiResp[FavoriteState])
@respond
async def unfavorite(
    content_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.interaction_favorite),
    db: AsyncSession = Depends(get_session),
) -> FavoriteState:
    return await remove_favorite(db, cur.id, content_id)


@router.get("/me/favorites", response_model=ApiResp[PageData[FavoriteItem]])
@respond
async def my_favorites(
    cur: CurrentUser = Depends(get_current_user),
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[FavoriteItem]:
    return await list_favorites(db, cur.id, page=pag.page, limit=pag.limit)


@router.post("/views/{content_id}", response_model=ApiResp[ViewState])
@respond
async def report_view(
    content_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.interaction_history),
    db: AsyncSession = Depends(get_session),
) -> ViewState:
    return await record_view(db, cur.id, content_id)


@router.get("/me/history", response_model=ApiResp[PageData[HistoryItem]])
@respond
async def my_history(
    cur: CurrentUser = Depends(get_current_user),
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[HistoryItem]:
    return await list_history(db, cur.id, page=pag.page, limit=pag.limit)
