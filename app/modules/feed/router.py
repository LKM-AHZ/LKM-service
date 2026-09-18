"""信息流(feed)域 REST 路由：关注/取消关注用户与版块 + 时间线 read 合流。

M2.3 汇集原 follow/router（user_follow_router：(/users...)与 board_follow_router
(/content/boards...)）+ 原 timeline/router（timeline_router：(/timeline...)）。三个
APIRouter 各自声明前缀，URL 契约（前端/集成测试续用）保持不破。

* 关注写/查：需要当前用户（关注/取关/列表），个别状态查询可用可缺席用户。
* 时间线：匿名仅 ``hot``；登录可按需 ``follow``（关注流）/ ``hot``（全站热门）。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.common import ApiResp, ListData
from app.core.config import settings
from app.core.err import respond
from app.core.wire import msgspec_ok
from app.db.session import get_read_session, get_session
from app.modules.auth.deps import (
    CurrentUser,
    get_current_user,
    get_optional_user,
)
from app.modules.feed import service as feed_service
from app.modules.feed.schemas import (
    FeedResponse,
    FollowBoard,
    FollowState,
    FollowToggle,
    FollowUser,
)
from app.modules.feed.service import get_timeline
from app.modules.feed.wire import to_wire

user_follow_router = APIRouter(prefix="/users", tags=["follow"])
board_follow_router = APIRouter(prefix="/content/boards", tags=["follow"])
timeline_router = APIRouter(prefix="/timeline", tags=["timeline"])


# ---------------------------------------------------------------------------
# 关注关系（原 follow）
# ---------------------------------------------------------------------------


@user_follow_router.post("/{user_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def follow_a_user(
    user_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await feed_service.follow_user(db, cur.id, user_id)
    return FollowToggle(following=True)


@user_follow_router.delete("/{user_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def unfollow_a_user(
    user_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await feed_service.unfollow_user(db, cur.id, user_id)
    return FollowToggle(following=False)


@board_follow_router.post("/{board_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def follow_a_board(
    board_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await feed_service.follow_board(db, cur.id, board_id)
    return FollowToggle(following=True)


@board_follow_router.delete("/{board_id}/follow", response_model=ApiResp[FollowToggle])
@respond
async def unfollow_a_board(
    board_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FollowToggle:
    await feed_service.unfollow_board(db, cur.id, board_id)
    return FollowToggle(following=False)


@user_follow_router.get("/me/following", response_model=ApiResp[ListData[FollowUser]])
@respond
async def my_following_users(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, list[FollowUser]]:
    rows = await feed_service.list_following_users(db, cur.id)
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
    following = await feed_service.is_following_user(db, cur.id, user_id)
    return FollowState(is_following=following)


@board_follow_router.get("/me/following", response_model=ApiResp[ListData[FollowBoard]])
@respond
async def my_following_boards(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, list[FollowBoard]]:
    rows = await feed_service.list_followed_boards(db, cur.id)
    return {"items": [FollowBoard(board_id=bid, title=title) for bid, title in rows]}


# ---------------------------------------------------------------------------
# 时间线 read 合流（原 timeline）
# ---------------------------------------------------------------------------


@timeline_router.get("", response_model=ApiResp[FeedResponse])
@respond
async def get_timeline_endpoint(
    cursor: str | None = Query(default=None),
    limit: int = Query(20, ge=1, le=100),
    mode: str = Query("follow", pattern="^(follow|hot)$"),
    cur: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_read_session),
) -> FeedResponse | Response:
    """时间线读热端点（§6.5.2）。

    ``response_model`` 仅用于 OpenAPI 文档：开启 ``read_msgspec_enabled`` 时返回已用 msgspec
    预编码的 ``Response``（FastAPI 对 Response 实例直接透传、不再跑 response_model 序列化），
    关闭时返回 Pydantic ``FeedResponse`` 走既有路径。两条路径 JSON 等价由契约测试守。
    """
    user_id = cur.id if cur is not None else None
    resp = await get_timeline(db, user_id=user_id, mode=mode, cursor=cursor, limit=limit)
    if settings.read_msgspec_enabled:
        return msgspec_ok(to_wire(resp))
    return resp
