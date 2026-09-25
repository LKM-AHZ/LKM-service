import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.common import (
    ApiResp,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
    parse_tags,
)
from app.core.config import settings
from app.core.err import BizError, CommonErr, respond
from app.core.wire import msgspec_ok
from app.db.session import get_read_session, get_session
from app.modules.admin.deps import require_admin_2fa
from app.modules.files.models import FileStatus, LibraryFile
from app.modules.files.schemas import (
    DownloadUrlInfo,
    FileCreate,
    FileInfo,
    UploadInitResp,
)
from app.modules.files.service import (
    bump_download,
    confirm_upload,
    delete_file,
    download_url,
    get_file,
    get_files_plan,
    list_files,
    review_file,
    serve_content,
    upload_init,
)
from app.modules.files.service import (
    create_file as create_file_service,
)
from app.modules.files.wire import to_wire
from app.modules.rbac.deps import RequirePermission
from app.modules.rbac.permissions import Permission, composible_role
from app.modules.rbac.service import check_owner, role_has_permission
from auth.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/status", response_model=ModuleStatus)
async def files_status() -> ModuleStatus:
    return ModuleStatus(
        module="files",
        status="implemented_minimal",
        responsibility="Manage shared academic files and downloads.",
        next_steps=get_files_plan()["next_steps"],
    )


@router.get("", response_model=ApiResp[PageData[FileInfo]])
@respond
async def get_files(
    pag: PaginateParams = Depends(PaginateDep()),
    category_id: str | None = Query(default=None, max_length=50),
    status: str | None = Query(default=None, max_length=20),
    sort: str = Query(default="newest"),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[FileInfo] | Response:
    page = await list_files(
        db,
        page=pag.page,
        limit=pag.limit,
        category_id=category_id,
        status=status,
        sort=sort,
    )
    if settings.read_msgspec_enabled:
        return msgspec_ok(to_wire(page))
    return page


@router.post("", response_model=ApiResp[FileInfo])
@respond
async def upload_file(
    file: UploadFile = File(...),
    # 长度约束要落在 Form 上：放进端点内的 FileCreate 里，超限时抛的是 pydantic
    # ValidationError，而 FastAPI 只把 RequestValidationError 转 422，其余会落到兜底
    # handler 变 500。这里与 schemas.FileCreate 的上界保持一致。
    category_id: str = Form(default="", max_length=50),
    description: str = Form(default="", max_length=500),
    tags: str = Form(default="[]"),
    cur: CurrentUser = RequirePermission(Permission.files_upload),
    db: AsyncSession = Depends(get_session),
) -> FileInfo:
    # 复用统一入口解析 form 里的 tags（JSON 串/畸形值均归一为 list[str]），
    # 不再各自 json.loads——前者会把非列表 JSON 变成端点内的 ValidationError
    tags_list = parse_tags(tags)

    # filename / content_type 来自 multipart 分段头，无法用 Form 约束；这里把超限
    # （original_name>255 / mime_type>100）转成受控的 INVALID_INPUT(422)，而不是 500。
    try:
        info = FileCreate(
            original_name=file.filename or "untitled",
            mime_type=file.content_type or "application/octet-stream",
            category_id=category_id,
            description=description,
            tags=tags_list,
        )
    except ValidationError as exc:
        raise BizError(CommonErr.INVALID_INPUT, str(exc)) from exc
    return await create_file_service(db, cur.id, info, file.file)


@router.post("/upload-init", response_model=ApiResp[UploadInitResp])
@respond
async def upload_init_endpoint(
    payload: FileCreate,
    cur: CurrentUser = RequirePermission(Permission.files_upload),
    db: AsyncSession = Depends(get_session),
) -> UploadInitResp:
    """预签名直传初始化：Local→sync，S3→direct（presigned PUT + upload_id）。"""
    return await upload_init(db, payload, cur)


@router.post("/{upload_id}/confirm", response_model=ApiResp[FileInfo])
@respond
async def confirm_upload_endpoint(
    upload_id: str,
    cur: CurrentUser = RequirePermission(Permission.files_upload),
    db: AsyncSession = Depends(get_session),
) -> FileInfo:
    """确认直传：回读对象→SHA3→去重→登记 PENDING。upload_id 为 str UUID。"""
    return await confirm_upload(db, upload_id, cur)


@router.get("/{file_id}", response_model=ApiResp[FileInfo])
@respond
async def get_file_detail(
    file_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> FileInfo:
    return await get_file(db, file_id, bump_view=True)


@router.post("/{file_id}/download", response_model=ApiResp[dict[str, Any]])
@respond
async def download_file(
    file_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.files_download),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"download_count": await bump_download(db, file_id)}


@router.post("/{file_id}/review", response_model=ApiResp[FileInfo])
@respond
async def review_uploaded_file(
    file_id: uuid.UUID,
    _cur: Annotated[CurrentUser, require_admin_2fa],
    status: FileStatus = Form(...),
    review_comment: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> FileInfo:
    """管理员审核文件：通过 / 驳回（驳回时删除物理文件并联动同 hash 条目）。

    高危破坏性操作（驳回删物理文件 + 发放积分）：先经 require_admin_2fa（后台会话 +
    1h 2FA 信任），再叠加 files_review 权限点；二者并取才放行，避免将来 files_review
    被授给非管理角色时"仅凭权限点即越权审核/删文件"。通过后 service 层仍走
    is_admin=True 跳过 account_level==admin 门槛（权限点已代管）。
    """
    role = composible_role(_cur.account_level, _cur.role)
    if not await role_has_permission(db, role, Permission.files_review):
        raise BizError(CommonErr.FORBIDDEN)
    return await review_file(
        db,
        file_id,
        target_status=status,
        review_comment=review_comment,
        is_admin=True,
    )


@router.post("/{file_id}/delete", response_model=ApiResp[FileInfo])
@respond
async def delete_uploaded_file(
    file_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FileInfo:
    """软删除文件：属主或持有 file.owner_delete 的管理员可操作。

    属主判定在 handler 内用 check_owner 谓词：凭 file.owner_delete 权限点（仅 super_admin
    持有）即可代管删任意文件，否则查 LibraryFile.uploader_id == cur.id（属主）。对象不存在
    或非属主 → 403。通过后传 is_admin=True 给 delete_file，跳过 service 内部的二次属主校验
    （否则 super_admin 凭权限点放行后仍会被 service 拦下，代删失效）。
    """
    await check_owner(
        db, cur, file_id, LibraryFile, "uploader_id", Permission.file_owner_delete
    )
    return await delete_file(
        db,
        file_id,
        actor_id=cur.id,
        is_admin=True,
    )


@router.get("/{file_id}/preview")
async def preview_file(
    file_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.files_download),
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """预览：仅 APPROVED 可访问，inline 流式返回，计 view_count。"""
    resp = await serve_content(db, file_id, "inline")
    # 依赖 teardown 要等响应体发完才跑，若不在此提前提交，慢客户端下载期间会一直占着
    # 池连接（生成器只读 storage，不再需要 DB）；提交同时落定上面的 view_count 计次。
    await db.commit()
    return resp


@router.get("/{file_id}/download/url", response_model=ApiResp[DownloadUrlInfo])
@respond
async def download_file_url(
    file_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.files_download),
    db: AsyncSession = Depends(get_session),
) -> DownloadUrlInfo:
    """签发下载 URL（本地→/content、S3→预签名），计 download_count。"""
    return await download_url(db, file_id, cur)


@router.get("/{file_id}/content")
async def download_file_content(
    file_id: uuid.UUID,
    cur: CurrentUser = RequirePermission(Permission.files_download),
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """附件流式下载：仅 APPROVED 可访问。"""
    resp = await serve_content(db, file_id, "attachment")
    # 同上：流式期间不再需要 DB，提前结束事务把连接还给连接池（本路径无写入，提交即空事务）
    await db.commit()
    return resp
