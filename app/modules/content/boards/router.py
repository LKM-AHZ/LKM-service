import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    bump_collection_version,
    cache_invalidate,
    make_key,
)
from app.core.common import ApiResp, ModuleStatus
from app.core.err import BizError, CommonErr, respond
from app.db.session import get_session
from app.modules.admin.deps import require_admin_2fa
from app.modules.content.boards.errors import (
    BoardErr,  # noqa: F401  (副作用注册已由 main 统一)
)
from app.modules.content.boards.schemas import (
    BanRequest,
    BoardApplicationCreate,
    BoardApplicationOut,
    BoardCreate,
    BoardOut,
    BoardUpdate,
    ReviewBoardApplicationRequest,
)
from app.modules.content.boards.service import (
    ban_user,
    create_board_ex,
    get_board_ex,
    review_application,
    submit_application,
    unban_user,
    update_board_ex,
)
from app.modules.content.models import Board
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission, composible_role
from app.modules.rbac.service import check_owner, role_has_permission
from auth.deps import CurrentUser, get_current_user

CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
# 危险操作（审核通过/驳回等破坏性写操作）：需已通过 2FA 且信任未过期（1 小时）；
# 2FA 之上再叠加 boards_review_application 权限点（handler 内判定）。
Admin2FADep = Annotated[CurrentUser, require_admin_2fa]


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="boards",
        status="implemented",
        responsibility="主题板块：帖子/专栏按板块组织，板块申请与负责人流，禁言与发言准入。",
        next_steps=[
            "前端板块页接线（forum/column 改用 boardId）",
            "积分/通知联动（阶段4）",
            "竞赛/QA/项目挂板块（后端未建）",
        ],
    )


router = APIRouter(prefix="/boards", tags=["content", "boards"])


@router.get("/status", response_model=ModuleStatus)
async def boards_status() -> ModuleStatus:
    return _status()


# 管理端
@router.post("", response_model=ApiResp[BoardOut])
@respond
async def admin_create_board(
    info: BoardCreate,
    _cur: CurrentUser = RequirePermission(Permission.boards_manage),
    db: AsyncSession = Depends(get_session),
) -> BoardOut:
    board = await create_board_ex(db, info, owner_id=None)
    await bump_collection_version("boards")
    return board


@router.post("/applications", response_model=ApiResp[BoardApplicationOut])
@respond
async def submit_app(
    info: BoardApplicationCreate,
    cur: CurrentUser = RequirePermission(Permission.boards_create_application),
    db: AsyncSession = Depends(get_session),
) -> BoardApplicationOut:
    return await submit_application(db, cur.id, info)


@router.post(
    "/applications/{app_id}/review", response_model=ApiResp[BoardApplicationOut]
)
@respond
async def review_app(
    app_id: uuid.UUID,
    body: ReviewBoardApplicationRequest,
    _cur: Admin2FADep,
    db: AsyncSession = Depends(get_session),
) -> BoardApplicationOut:
    # Admin2FADep 已保证 admin 会话 + 2FA 信任；此处叠加 boards_review_application
    # 权限点（super_admin 有，org_member 无）。校验失败按 FORBIDDEN 返回。
    role = composible_role(_cur.account_level, _cur.role)
    if not await role_has_permission(db, role, Permission.boards_review_application):
        raise BizError(CommonErr.FORBIDDEN)
    result = await review_application(db, app_id, _cur.id, body)
    await bump_collection_version("boards")
    return result


# 负责人
@router.patch("/{board_id}", response_model=ApiResp[BoardOut])
@respond
async def owner_update_board(
    board_id: uuid.UUID,
    patch: BoardUpdate,
    cur: CurrentUserDep,
    db: AsyncSession = Depends(get_session),
) -> BoardOut:
    # 对象级权限：板块属主放行，或拥有 board_owner_manage（super_admin 代管）放行。
    await check_owner(
        db, cur, board_id, Board, "owner_id", Permission.board_owner_manage
    )
    result = await update_board_ex(
        db, board_id, cur.id, patch, is_admin=(cur.role == "super_admin")
    )
    await bump_collection_version("boards")
    await cache_invalidate(make_key("boards:item", board_id))
    return result


@router.post("/{board_id}/bans", response_model=ApiResp[dict[str, bool]])
@respond
async def ban(
    board_id: uuid.UUID,
    body: BanRequest,
    cur: CurrentUserDep,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await check_owner(
        db, cur, board_id, Board, "owner_id", Permission.board_owner_manage
    )
    board = await get_board_ex(db, board_id)
    await ban_user(db, board, cur.id, body, is_admin=(cur.role == "super_admin"))
    return {"ok": True}


@router.delete(
    "/{board_id}/bans/{target_user_id}", response_model=ApiResp[dict[str, bool]]
)
@respond
async def unban(
    board_id: uuid.UUID,
    target_user_id: uuid.UUID,
    cur: CurrentUserDep,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    await check_owner(
        db, cur, board_id, Board, "owner_id", Permission.board_owner_manage
    )
    board = await get_board_ex(db, board_id)
    await unban_user(
        db, board, cur.id, target_user_id, is_admin=(cur.role == "super_admin")
    )
    return {"ok": True}
