"""notification REST：站内信列表/未读数/已读、偏好开关、推送 token 注册。

读操作用 ``notification.read``，写操作用 ``notification.manage``；两者都只允许操作
「自己的」数据（user_id 一律取自 token，端点不接受客户端传入 user_id）。
"""

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
from app.modules.auth.deps import CurrentUser
from app.modules.notification.schemas import (
    MarkReadIn,
    MarkReadOut,
    NotificationOut,
    PreferencesIn,
    PreferencesOut,
    TokenIn,
    TokenOut,
    UnreadCountOut,
)
from app.modules.notification.service import (
    delete_token,
    list_notifications,
    list_preferences,
    mark_read,
    register_token,
    set_preferences,
    unread_count,
)
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission

router = APIRouter(prefix="/notification", tags=["notification"])


@router.get("/status", response_model=ModuleStatus)
async def notification_status() -> ModuleStatus:
    return ModuleStatus(
        module="notification",
        status="implemented",
        responsibility="站内信：业务事件生成通知（含同类聚合）+ WS 实时推送 + 偏好门控 + 推送 token。",
        next_steps=[
            "推送 token 的实际下发通道（APNs/FCM）待选型",
            "更多事件源接入（feed 关注、审核结果）",
        ],
    )


@router.get("/me", response_model=ApiResp[PageData[NotificationOut]])
@respond
async def my_notifications(
    cur: CurrentUser = RequirePermission(Permission.notification_read),
    pag: PaginateParams = Depends(PaginateDep()),
    unread: bool = Query(False, description="只看未读"),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[NotificationOut]:
    return await list_notifications(
        db, cur.id, page=pag.page, limit=pag.limit, unread_only=unread
    )


@router.get("/me/unread-count", response_model=ApiResp[UnreadCountOut])
@respond
async def my_unread_count(
    cur: CurrentUser = RequirePermission(Permission.notification_read),
    db: AsyncSession = Depends(get_read_session),
) -> UnreadCountOut:
    return UnreadCountOut(unread=await unread_count(db, cur.id))


@router.post("/me/read", response_model=ApiResp[MarkReadOut])
@respond
async def mark_my_read(
    body: MarkReadIn,
    cur: CurrentUser = RequirePermission(Permission.notification_manage),
    db: AsyncSession = Depends(get_session),
) -> MarkReadOut:
    updated = await mark_read(db, cur.id, ids=body.ids, all_=body.all)
    return MarkReadOut(updated=updated)


@router.get("/me/preferences", response_model=ApiResp[PreferencesOut])
@respond
async def my_preferences(
    cur: CurrentUser = RequirePermission(Permission.notification_read),
    db: AsyncSession = Depends(get_read_session),
) -> PreferencesOut:
    return PreferencesOut(items=await list_preferences(db, cur.id))


@router.put("/me/preferences", response_model=ApiResp[PreferencesOut])
@respond
async def update_my_preferences(
    body: PreferencesIn,
    cur: CurrentUser = RequirePermission(Permission.notification_manage),
    db: AsyncSession = Depends(get_session),
) -> PreferencesOut:
    items = [(i.type, i.enabled) for i in body.items]
    return PreferencesOut(items=await set_preferences(db, cur.id, items))


@router.post("/me/tokens", response_model=ApiResp[TokenOut])
@respond
async def register_my_token(
    body: TokenIn,
    cur: CurrentUser = RequirePermission(Permission.notification_manage),
    db: AsyncSession = Depends(get_session),
) -> TokenOut:
    return await register_token(db, cur.id, body.token, body.platform)


@router.delete("/me/tokens", response_model=ApiResp[MarkReadOut])
@respond
async def delete_my_token(
    token: str = Query(..., min_length=1, max_length=255),
    cur: CurrentUser = RequirePermission(Permission.notification_manage),
    db: AsyncSession = Depends(get_session),
) -> MarkReadOut:
    return MarkReadOut(updated=await delete_token(db, cur.id, token))
