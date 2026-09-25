"""信息流(feed)域 REST：时间线 read 合流。

**关注关系路由已迁出**（``user_follow_router`` → ``/users/...``、``board_follow_router`` →
``/content/boards/...``，现由 ``app.modules.interaction.router`` 提供，URL 一字不变）。
本文件只剩 ``timeline_router``（``/timeline``）。

时间线：匿名仅 ``hot``；登录可按需 ``follow``（关注流）/ ``hot``（全站热门）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.common import ApiResp
from app.core.config import settings
from app.core.err import respond
from app.core.wire import msgspec_ok
from app.db.session import get_read_session
from app.modules.feed.schemas import FeedResponse
from app.modules.feed.service import get_timeline
from app.modules.feed.wire import to_wire
from auth.deps import CurrentUser, get_optional_user

timeline_router = APIRouter(prefix="/timeline", tags=["timeline"])


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
    resp = await get_timeline(
        db, user_id=user_id, mode=mode, cursor=cursor, limit=limit
    )
    if settings.read_msgspec_enabled:
        return msgspec_ok(to_wire(resp))
    return resp
