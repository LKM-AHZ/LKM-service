"""interaction REST：收藏/取消/我的收藏 + 浏览上报/我的浏览历史 + 关注关系。

写操作走权限点（interaction.favorite / interaction.history），读自己的列表只要求登录。
关注路由（``user_follow_router`` / ``board_follow_router``）原属 feed 域，按蓝图 §7.2
目标形态迁入本模块——**URL 前缀一字不变**（``/users/...``、``/content/boards/...``）。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.common import (
    ApiResp,
    ListData,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
)
from app.core.config import settings
from app.core.err import respond
from app.core.wire import msgspec_ok
from app.db.session import get_read_session, get_session
from app.modules.interaction import service as interaction_service
from app.modules.interaction.schemas import (
    FavoriteItem,
    FavoriteState,
    FollowBoard,
    FollowState,
    FollowToggle,
    FollowUser,
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
from app.modules.interaction.wire import favorites_to_wire, history_to_wire
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission
from auth.deps import CurrentUser, get_current_user, get_optional_user

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
) -> PageData[FavoriteItem] | Response:
    page = await list_favorites(db, cur.id, page=pag.page, limit=pag.limit)
    if settings.read_msgspec_enabled:
        return msgspec_ok(favorites_to_wire(page))
    return page


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
) -> PageData[HistoryItem] | Response:
    page = await list_history(db, cur.id, page=pag.page, limit=pag.limit)
    if settings.read_msgspec_enabled:
        return msgspec_ok(history_to_wire(page))
    return page


# ---------------------------------------------------------------------------
# 关注关系（原 feed 域的 follow 路由，URL 前缀一字不变）
# ---------------------------------------------------------------------------

user_follow_router = APIRouter(prefix="/users", tags=["follow"])
board_follow_router = APIRouter(prefix="/content/boards", tags=["follow"])


@user_follow_router.post("/{user_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def follow_a_user(
    user_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await interaction_service.follow_user(db, cur.id, user_id)
    return FollowToggle(following=True)


@user_follow_router.delete("/{user_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def unfollow_a_user(
    user_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await interaction_service.unfollow_user(db, cur.id, user_id)
    return FollowToggle(following=False)


@board_follow_router.post("/{board_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def follow_a_board(
    board_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await interaction_service.follow_board(db, cur.id, board_id)
    return FollowToggle(following=True)


@board_follow_router.delete("/{board_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def unfollow_a_board(
    board_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await interaction_service.unfollow_board(db, cur.id, board_id)
    return FollowToggle(following=False)


@user_follow_router.get("/me/following", response_model=ApiResp[ListData[FollowUser]])
@respond
async def my_following_users(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, list[FollowUser]]:
    rows = await interaction_service.list_following_users(db, cur.id)
    return {
        "items": [
            FollowUser(user_id=uid, display_name=name, avatar=avatar)
            for uid, name, avatar in rows
        ]
    }


@user_follow_router.get("/{user_id}/follow/status", response_model=ApiResp[FollowState])
@respond
async def user_follow_status(
    user_id: uuid.UUID,
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_session),
) -> FollowState:
    if cur is None:
        return FollowState(is_following=False)
    following = await interaction_service.is_following_user(db, cur.id, user_id)
    return FollowState(is_following=following)


@board_follow_router.get("/me/following", response_model=ApiResp[ListData[FollowBoard]])
@respond
async def my_following_boards(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, list[FollowBoard]]:
    rows = await interaction_service.list_followed_boards(db, cur.id)
    return {"items": [FollowBoard(board_id=bid, title=title) for bid, title in rows]}
