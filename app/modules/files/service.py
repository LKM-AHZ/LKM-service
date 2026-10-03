import asyncio
import hashlib
import io
import json
import logging
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, NoReturn, Protocol
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select

from app.modules.files.errors import FileErr
from app.modules.files.models import FILES_TABLE_PLAN, FileStatus, LibraryFile
from app.modules.files.processing import is_office, process_upload
from app.modules.files.repository import (
    LibraryFileRepository,
    UploadSessionRepository,
)
from app.modules.files.retention import (
    archive_exists,
    backup_file,
    delete_archive,
    read_archive,
)
from app.modules.files.schemas import (
    DownloadUrlInfo,
    FileCreate,
    FileInfo,
    UploadInitResp,
)
from app.modules.points.rules import enqueue_points_event
from app.modules.projects.models import Project, ProjectMember
from core.common import PageData, paginate_offset, paginate_pages, parse_tags
from core.config import settings
from core.contracts import CurrentUser
from core.db.outbox import enqueue_outbox
from core.db.repo import get_or_raise
from core.db.repository import DbSession
from core.err import BizError
from core.messaging import RKEY_FILE_CHANGED
from core.ports.snapshot import get_user_snapshot_batch
from core.secrets import reveal
from core.storage.base import StorageBackend
from core.storage.errors import StorageErr
from core.storage.factory import get_storage

logger = logging.getLogger(__name__)


async def _enqueue_file_change(db: DbSession, file_id: uuid.UUID) -> None:
    if settings.search_sync_enabled:
        await enqueue_outbox(
            db,
            RKEY_FILE_CHANGED,
            {"fn": "apply_file_event", "args": [str(file_id)]},
        )


class _Readable(Protocol):
    """可同步分块读取的 file-like 对象最小协议。"""

    def read(self, size: int = -1, /) -> bytes: ...


def get_files_plan() -> dict[str, Any]:
    return {
        "status": "implemented",
        "tables": FILES_TABLE_PLAN,
        "next_steps": [
            "Configure ClamAV and sensitive-term policy before production uploads",
            "Configure an off-host backup for disaster recovery",
            "Configure S3 lifecycle transitions for cold object storage",
        ],
    }


def _file_to_schema(f: LibraryFile, uploader_name: str) -> FileInfo:
    return FileInfo.model_validate(f).model_copy(
        update={"uploader_name": uploader_name}
    )


async def _uploader_map(
    db: DbSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(set(user_ids)))
    return {uid: s.display_name for uid, s in snaps.items()}


async def upload_projects(db: DbSession, user_id: uuid.UUID) -> list[dict[str, str]]:
    member = (
        select(ProjectMember.id)
        .where(ProjectMember.project_id == Project.id, ProjectMember.user_id == user_id)
        .exists()
    )
    rows = (
        await db.execute(
            select(Project.id, Project.title)
            .where(or_(Project.applicant_id == user_id, member))
            .order_by(Project.created_at.desc())
        )
    ).all()
    return [{"id": str(row.id), "title": row.title} for row in rows]


async def list_files(
    db: DbSession,
    page: int = 1,
    limit: int = 20,
    category_id: str | None = None,
    status: str | None = None,
    sort: str = "newest",
    viewer: CurrentUser | None = None,
    enforce_visibility: bool = False,
) -> PageData[FileInfo]:
    repo = LibraryFileRepository(db)
    total = await repo.count_page(
        category_id=category_id,
        status=status,
        viewer=viewer,
        enforce_visibility=enforce_visibility,
    )
    files = await repo.list_page(
        category_id=category_id,
        status=status,
        sort=sort,
        offset=paginate_offset(page, limit),
        limit=limit,
        viewer=viewer,
        enforce_visibility=enforce_visibility,
    )

    names = await _uploader_map(db, [f.uploader_id for f in files])
    items = [_file_to_schema(f, names.get(f.uploader_id, "")) for f in files]
    return PageData(
        items=items, total=total, page=page, pages=paginate_pages(total, limit)
    )


async def get_file(
    db: DbSession,
    file_id: uuid.UUID,
    bump_view: bool = False,
    viewer: CurrentUser | None = None,
    enforce_visibility: bool = False,
) -> FileInfo:
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    if enforce_visibility:
        await _require_visible(db, f, viewer)

    view_count = (
        await LibraryFileRepository(db).increment_view(file_id) if bump_view else None
    )

    names = await _uploader_map(db, [f.uploader_id])
    info = _file_to_schema(f, names.get(f.uploader_id, ""))
    return (
        info.model_copy(update={"view_count": view_count})
        if view_count is not None
        else info
    )


_CHUNK = 1024 * 1024  # 分块读写，避免整文件载入内存


def _buffer_and_hash(stream: _Readable, limit: int) -> tuple[int, str, IO[bytes]]:
    """单遍读 ``stream``：一边算 SHA3-256、一边把内容 spool 到临时文件，返回可重读流。

    相比原 ``io.BytesIO``（整文件驻留内存，上限=整个上传大小，大文件有 OOM 风险）
    改为 ``tempfile.TemporaryFile``：小文件落在内衬缓冲区（快），大文件自动 spill 到
    磁盘，内存峰值钉在分块大小。因为哈希必须在落盘前算出（去重策略），单遍流不能读
    两次，故在此缓冲供 ``storage.save`` 复用。超过 ``limit`` 立即抛 ``TOO_LARGE``。
    调用方用完须 ``close()``（TemporaryFile 关闭即自动删除）。
    """
    hasher = hashlib.sha3_256()
    buf = tempfile.TemporaryFile()  # noqa: SIM115  # 需跨函数返回给 storage.save 复用，不能用 with
    total = 0
    try:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise BizError(
                    FileErr.TOO_LARGE,
                    detail=f"Upload exceeds {limit} byte limit",
                )
            hasher.update(chunk)
            buf.write(chunk)
        buf.seek(0)
        return total, hasher.hexdigest(), buf
    except BaseException:
        buf.close()
        raise


def _content_path(sha3_hash: str) -> Path:
    """内容寻址逻辑路径（Local 根下）：``files_store_dir/<hash[:2]>/<hash>``，一层分桶，同内容同路径。"""
    return Path(settings.files_store_dir) / sha3_hash[:2] / sha3_hash


def _build_bucket_key(content_hash: str) -> str:
    """逻辑存储 key：``<hash[:2]>/<hash>``。Local/S3 同形（均不带 ``files/`` 前缀，
    S3Storage 内部会拼接其 ``prefix``，避免 ``files/files/...`` 双头）。"""
    return f"{content_hash[:2]}/{content_hash}"


def _bucket_key_of(f: LibraryFile) -> str | None:
    """该条目引用的逻辑 key；无哈希时无法定位（返回 None）。"""
    return _build_bucket_key(f.sha3_hash) if f.sha3_hash else None


_INLINE_SAFE_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "audio/mpeg",
        "audio/ogg",
        "video/mp4",
        "video/webm",
    }
)


def _storage_path_for(content_hash: str) -> str:
    """构造与首写时后端实际返回一致的 ``storage_path``，避免元数据漂移。

    Local：root 下 ``_content_path`` 的规范绝对路径（``.resolve()`` 匹配首写形态）；
    S3：复用其 ``prefix`` 语义（``prefix/<hash[:2]>/<hash>``）。普通上传与直传登记共用。
    """
    if settings.storage_backend == "s3":
        return f"{settings.s3_prefix}/{_build_bucket_key(content_hash)}"
    return str(_content_path(content_hash).resolve())


_storage_sig: tuple[object, ...] = ()


def _get_storage() -> StorageBackend:
    """按当前 ``settings`` 取后端；相关配置在测试中会被 monkeypatch，故配置变化时让工厂
    重建，避免拿到缓存中旧 root 的后端。生产配置恒定 → ``cache_clear`` 不触发，等同单例。"""
    global _storage_sig
    sig = (
        settings.storage_backend,
        settings.files_store_dir,
        settings.s3_endpoint_url,
        settings.s3_region,
        settings.s3_bucket,
        reveal(settings.s3_access_key),
        reveal(settings.s3_secret_key),
        settings.s3_prefix,
    )
    if sig != _storage_sig:
        get_storage.cache_clear()
        _storage_sig = sig
    return get_storage()


def _raise_storage_as_file(exc: BizError) -> NoReturn:
    """把 storage 层抛的 ``BizError(StorageErr.*)`` 转成 files 既有 ``FileErr``，保持前端契约。

    ``FileErr`` 定义不改：StorageErr.STORE_ERROR→FileErr.STORE_ERROR(500)、
    TOO_LARGE→FileErr.TOO_LARGE(413)、NOT_FOUND→FileErr.NOT_FOUND(404)。
    """
    if exc.errcode == StorageErr.TOO_LARGE:
        raise BizError(FileErr.TOO_LARGE, detail=exc.detail) from exc
    if exc.errcode == StorageErr.NOT_FOUND:
        raise BizError(FileErr.NOT_FOUND, detail=exc.detail) from exc
    # STORE_ERROR 及未知错误统一归为存储失败(500)
    raise BizError(FileErr.STORE_ERROR, detail=exc.detail) from exc


def _make_stored_name(original_name: str) -> str:
    """生成唯一展示/定位名（存储层按内容哈希去重、共享物理文件，此名仅唯一）。"""
    suffix = Path(original_name).suffix[:32]
    return f"{uuid.uuid4().hex}{suffix}"


async def _identity_for_upload(
    db: DbSession,
    repo: LibraryFileRepository,
    uploader_id: uuid.UUID,
    info: FileCreate,
) -> tuple[str, int, uuid.UUID | None, str, uuid.UUID | None]:
    """验证项目归属及版本链，并在事务锁下分配编号/版次。"""
    if info.version_of is not None:
        parent = await get_or_raise(
            db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == info.version_of
        )
        if parent.uploader_id != uploader_id:
            raise BizError(FileErr.NOT_OWNER)
        if parent.status in (FileStatus.DELETED, FileStatus.REJECTED):
            raise BizError(FileErr.INVALID_STATUS)
        if not parent.document_code:
            raise BizError(FileErr.INVALID_STATUS, detail="Document has no code")
        info.category_id = parent.category_id
        info.description = info.description or parent.description
        info.tags = info.tags or parse_tags(parent.tags)
        return (
            parent.document_code,
            await repo.next_version(parent.document_code),
            parent.root_file_id or parent.id,
            parent.classification,
            parent.project_id,
        )
    if info.project_id is not None:
        project = await db.scalar(select(Project).where(Project.id == info.project_id))
        member = await db.scalar(
            select(ProjectMember.id).where(
                ProjectMember.project_id == info.project_id,
                ProjectMember.user_id == uploader_id,
            )
        )
        if project is None or (project.applicant_id != uploader_id and member is None):
            raise BizError(FileErr.INVALID_PROJECT)
    code = await repo.next_document_code(datetime.now(UTC).year)
    return code, 1, None, info.classification, info.project_id


async def create_file(
    db: DbSession,
    uploader_id: uuid.UUID,
    info: FileCreate,
    stream: _Readable,
    max_bytes: int | None = None,
) -> FileInfo:
    """把上传流交给 storage 层落盘（内容寻址去重）并登记元数据。

    ``stream`` 需提供 ``read(n)``（可同步 File 对象）。累计超过 ``max_bytes``（默认取配置值）
    立即中止并抛 ``FileErr.TOO_LARGE``（413），不留任何落盘残留。

    内容寻址去重策略保留在 files 层：先算 SHA3-256 得到 ``bucket_key``，用 ``storage.exists``
    判断同内容是否已存在（跨 Local/S3 通用）；存在则复用不重写，缺失才 ``storage.save``。
    StorageErr → FileErr 转换保证前端契约不变。ref_count 仍在 DB 聚合，供删除/清理断言。
    """
    limit = settings.max_upload_bytes if max_bytes is None else max_bytes
    # 读流 + SHA3 + 写 spool 全是同步阻塞 I/O（最大可达 max_upload_bytes），
    # 直接在事件循环里跑会把整个 worker 的其他请求一起卡住 → 丢线程池；
    # buf 只是普通文件对象，跨线程交回后调用方照常 close/读取
    total, content_hash, buf = await asyncio.to_thread(_buffer_and_hash, stream, limit)
    bucket_key = _build_bucket_key(content_hash)

    repo = LibraryFileRepository(db)
    try:
        extracted_text, preview_pdf = await asyncio.to_thread(
            process_upload,
            buf,
            original_name=info.original_name,
            description=info.description,
            size=total,
        )
        code, version, root_id, classification, project_id = await _identity_for_upload(
            db, repo, uploader_id, info
        )
        # 事务锁覆盖物理写入和元数据登记。
        await repo.lock_hash(content_hash)
    except BaseException:
        buf.close()
        raise
    saved: dict[str, object] | None = None
    try:
        try:
            storage = _get_storage()
            if not await storage.exists(bucket_key):
                saved = dict(
                    await storage.save(buf, max_bytes=limit, bucket_key=bucket_key)
                )
        except BizError as exc:
            _raise_storage_as_file(exc)
    finally:
        # buf 是 _buffer_and_hash 的 spool 临时文件，用完即关（关闭自动删除，释放磁盘）。
        buf.close()

    if preview_pdf is not None:
        try:
            await _get_storage().save(
                io.BytesIO(preview_pdf),
                max_bytes=settings.files_preview_max_bytes,
                bucket_key=f"{bucket_key}.preview.pdf",
            )
        except BizError:
            logger.warning("文件 PDF 预览保存失败 hash=%s", content_hash, exc_info=True)

    if saved is not None:
        storage_path = str(saved["storage_path"])
    else:
        storage_path = _storage_path_for(content_hash)

    f = LibraryFile(
        uploader_id=uploader_id,
        original_name=info.original_name,
        stored_name=_make_stored_name(info.original_name),
        sha3_hash=content_hash,
        ref_count=1,
        storage_path=storage_path,
        mime_type=info.mime_type,
        size=total,
        category_id=info.category_id,
        document_code=code,
        version=version,
        root_file_id=root_id,
        classification=classification,
        project_id=project_id,
        extracted_text=extracted_text,
        description=info.description,
        tags=json.dumps(info.tags, ensure_ascii=False),
    )
    # 数据库失败时保留内容寻址对象供重试复用。此处可能已处于 failed transaction，
    # 再查询引用数并删除 blob 会覆盖原异常，也可能误删其他条目仍引用的对象。
    await repo.add(f)
    await repo.flush()
    if root_id is None:
        f.root_file_id = f.id
    await repo.sync_ref_count(content_hash)

    names = await _uploader_map(db, [f.uploader_id])
    return _file_to_schema(f, names.get(f.uploader_id, ""))


async def bump_download(db: DbSession, file_id: uuid.UUID) -> int:
    return await LibraryFileRepository(db).increment_download(file_id)


async def _require_visible(
    db: DbSession, f: LibraryFile, viewer: CurrentUser | None
) -> None:
    if viewer is not None and (
        viewer.account_level == "admin" or f.uploader_id == viewer.id
    ):
        return
    if f.status != FileStatus.APPROVED:
        raise BizError(FileErr.NOT_FOUND)
    if f.classification == "public":
        return
    if viewer is None:
        raise BizError(FileErr.NOT_FOUND)
    if f.classification == "internal":
        return
    if f.project_id and await db.scalar(
        select(ProjectMember.id).where(
            ProjectMember.project_id == f.project_id,
            ProjectMember.user_id == viewer.id,
        )
    ):
        return
    raise BizError(FileErr.NOT_FOUND)


async def list_versions(
    db: DbSession, file_id: uuid.UUID, viewer: CurrentUser | None
) -> list[FileInfo]:
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    await _require_visible(db, f, viewer)
    if not f.document_code:
        return [_file_to_schema(f, "")]
    rows = await LibraryFileRepository(db).versions(f.document_code)
    visible: list[LibraryFile] = []
    for row in rows:
        try:
            await _require_visible(db, row, viewer)
        except BizError:
            continue
        visible.append(row)
    names = await _uploader_map(db, [r.uploader_id for r in visible])
    return [_file_to_schema(r, names.get(r.uploader_id, "")) for r in visible]


async def review_file(
    db: DbSession,
    file_id: uuid.UUID,
    target_status: FileStatus,
    review_comment: str | None = None,
    is_admin: bool = False,
) -> FileInfo:
    """管理员审核文件：通过或驳回；共享同一内容的其他文件独立审核。"""
    if not is_admin:
        raise BizError(FileErr.STORE_ERROR, detail="Only admin can review files")
    if target_status not in (FileStatus.APPROVED, FileStatus.REJECTED):
        raise BizError(FileErr.INVALID_STATUS, detail="Invalid review status")

    repo = LibraryFileRepository(db)
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    if f.sha3_hash:
        await repo.lock_hash(f.sha3_hash)
    f = await repo.get_locked(file_id)
    if f is None:
        raise BizError(FileErr.NOT_FOUND, detail="File not found")
    if f.status != FileStatus.PENDING:
        raise BizError(FileErr.NOT_PENDING, detail="File is not pending")

    f.status = target_status
    f.review_comment = review_comment

    if target_status == FileStatus.REJECTED and f.sha3_hash:
        await repo.flush()
        await repo.sync_ref_count(f.sha3_hash)
        if await repo.count_live_by_hash(f.sha3_hash) == 0:
            await _delete_content_variants(f.sha3_hash)
    else:
        await repo.flush()

    # 仅审核通过时给归属者加分（f.status 已设为 target_status）
    if f.status == FileStatus.APPROVED:
        if f.sha3_hash:
            await backup_file(
                _get_storage(), _build_bucket_key(f.sha3_hash), f.sha3_hash
            )
            if settings.files_backup_dir:
                f.backed_up_at = datetime.now(UTC)
        await enqueue_points_event(db, f.uploader_id, "file_approved", f"file:{f.id}")
        await _enqueue_file_change(db, f.id)
        if f.document_code:
            for older in await repo.versions(f.document_code):
                if older.id != f.id and older.status == FileStatus.APPROVED:
                    await _enqueue_file_change(db, older.id)

    names = await _uploader_map(db, [f.uploader_id])
    return _file_to_schema(f, names.get(f.uploader_id, ""))


async def delete_file(
    db: DbSession,
    file_id: uuid.UUID,
    actor_id: uuid.UUID,
    is_admin: bool = False,
) -> FileInfo:
    """软删除文件：管理员或文件所有者可操作，物理文件引用归零时清理磁盘。"""
    repo = LibraryFileRepository(db)
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    if f.sha3_hash:
        await repo.lock_hash(f.sha3_hash)
    f = await repo.get_locked(file_id)
    if f is None:
        raise BizError(FileErr.NOT_FOUND)
    if not is_admin and f.uploader_id != actor_id:
        raise BizError(FileErr.STORE_ERROR, detail="Not the owner of this file")

    if f.status == FileStatus.DELETED:
        names = await _uploader_map(db, [f.uploader_id])
        return _file_to_schema(f, names.get(f.uploader_id, ""))

    old_hash = f.sha3_hash
    f.status = FileStatus.DELETED
    await repo.flush()
    await _enqueue_file_change(db, f.id)
    if f.document_code:
        for older in await repo.versions(f.document_code):
            if older.id != f.id and older.status == FileStatus.APPROVED:
                await _enqueue_file_change(db, older.id)

    if old_hash:
        remaining = await repo.count_live_by_hash(old_hash)
        await repo.sync_ref_count(old_hash)
        # 事务锁持有至 commit；此时无存活引用才删除物理对象。
        if remaining <= 0:
            await _delete_content_variants(old_hash)

    names = await _uploader_map(db, [f.uploader_id])
    return _file_to_schema(f, names.get(f.uploader_id, ""))


async def _delete_content_variants(content_hash: str) -> None:
    key = _build_bucket_key(content_hash)
    for suffix in (".thumb.webp", ".medium.webp", ".preview.pdf", ""):
        try:
            await _get_storage().delete(f"{key}{suffix}")
        except BizError as exc:
            if exc.errcode != StorageErr.NOT_FOUND:
                _raise_storage_as_file(exc)
    await delete_archive(content_hash)


def _require_approved(f: LibraryFile, *, action: str) -> None:
    """非 APPROVED 文件一律拒绝预览/下载，抛 403 NOT_APPROVED。"""
    if f.status != FileStatus.APPROVED:
        raise BizError(
            FileErr.NOT_APPROVED, detail=f"Cannot {action} non-approved file"
        )


async def download_url(
    db: DbSession, file_id: uuid.UUID, cur: CurrentUser
) -> DownloadUrlInfo:
    """签发下载 URL：本地后端回指 /content 端点，S3 后端返回预签名 URL（60s）。计次 download_count。"""
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    await _require_visible(db, f, cur)
    _require_approved(f, action="download")
    key = _bucket_key_of(f)
    if key is None:
        raise BizError(FileErr.NOT_FOUND, detail="File has no stored content")
    await LibraryFileRepository(db).increment_download(file_id)
    storage = _get_storage()
    if settings.storage_backend == "s3" and (
        not f.sha3_hash or not await archive_exists(f.sha3_hash)
    ):
        url = storage.presign_download(key, expires=60)
        return DownloadUrlInfo(kind="presigned", url=url, expires_in=60)
    return DownloadUrlInfo(kind="backend", url=f"/api/v1/files/{file_id}/content")


def _serve(
    db: DbSession,
    f: LibraryFile,
    disposition: Literal["inline", "attachment"],
    *,
    key: str | None = None,
    preview_pdf: bool = False,
    use_archive: bool = False,
) -> StreamingResponse:
    """构造流式响应：逐块读取 storage 字节，不整载内存；存储错误映射为 FileErr。"""

    async def it() -> AsyncIterator[bytes]:
        try:
            source = (
                read_archive(f.sha3_hash)
                if use_archive and f.sha3_hash
                else _get_storage().open(key or _bucket_key_of(f) or "")
            )
            async for chunk in source:
                yield chunk
        except BizError as exc:
            # 响应头已发出（Starlette 先发 start 再迭代 body），此处只能尽力收尾，
            # 无法再变成 404/500——真正的「对象缺失」由 serve_content 的预检拦截
            _raise_storage_as_file(exc)

    media_type = "application/pdf" if preview_pdf else f.mime_type
    if disposition == "inline" and media_type not in _INLINE_SAFE_TYPES:
        # mime_type 来自上传方（UploadFile.content_type / 直传 payload），内联渲染
        # text/html、image/svg+xml 这类可执行脚本的类型会在 API 源上形成存储型 XSS
        # （同源 cookie 可直接打接口）→ 不在白名单就降级为附件下载
        disposition = "attachment"
        media_type = "application/octet-stream"

    display_name = (
        f"{Path(f.original_name).stem}.pdf" if preview_pdf else f.original_name
    )
    ascii_fallback = (
        display_name.encode("ascii", "ignore").decode("ascii") or "download"
    )
    cd = (
        f"{disposition}; filename={ascii_fallback}; "
        f"filename*=UTF-8''{quote(display_name)}"
    )
    headers = {
        "Content-Disposition": cd,
        "Cache-Control": "private, no-store",
        # 禁内容嗅探（老浏览器可能把八位字节流嗅探成 HTML）
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(it(), media_type=media_type, headers=headers)


async def serve_content(
    db: DbSession,
    file_id: uuid.UUID,
    disposition: Literal["inline", "attachment"],
    viewer: CurrentUser | None = None,
) -> StreamingResponse:
    """预览(/preview)/下载(/content)共用入口：仅 APPROVED 可访问；预览计次 view_count。"""
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    await _require_visible(db, f, viewer)
    _require_approved(f, action="preview" if disposition == "inline" else "download")
    key = _bucket_key_of(f)
    if key is None:
        raise BizError(FileErr.NOT_FOUND, detail="File has no storage key")
    converted = False
    if disposition == "inline" and is_office(f.original_name):
        key = f"{key}.preview.pdf"
        converted = True
    # 预检对象存在性：响应头一旦发出，生成器里的存储错误无法再变成 404/500
    # （客户端会收到 200 + 截断体）。这里先探一次，把缺失/failed blob 拦在响应之前。
    hot_exists = await _get_storage().exists(key)
    use_archive = bool(
        not hot_exists
        and not converted
        and f.sha3_hash
        and await archive_exists(f.sha3_hash)
    )
    if not hot_exists and not use_archive:
        if converted:
            raise BizError(FileErr.PREVIEW_UNAVAILABLE)
        raise BizError(FileErr.NOT_FOUND, detail="Stored object not found")
    if disposition == "inline":  # 预览计次 view
        await LibraryFileRepository(db).increment_view(file_id)
    return _serve(
        db,
        f,
        disposition,
        key=key,
        preview_pdf=converted,
        use_archive=use_archive,
    )


# ---- Phase 2-B: 预签名直传（upload-init / confirm，Redis 标记 + 回读哈希去重） ----

_UPLOAD_TTL = 3600  # 会话"年龄窗口"1h：清扫按 upload_sessions.created_at 判龄
_PRESIGN_EXPIRES = 900  # presigned PUT 15min


async def upload_init(
    db: DbSession, info: FileCreate, cur: CurrentUser
) -> UploadInitResp:
    """预签名直传初始化。

    Local→``mode=sync``（前端回退 multipart POST /files，无 upload_id/URL）；
    S3→``mode=direct``，生成独立随机 key（``up/<uuid>``）+ 预签名 PUT URL，并把元数据落到
    ``upload_sessions``（供 confirm 登记用）。

    **元数据落 DB 而非 Redis**：原先存无 TTL 的 Redis 键，关掉 Redis 持久化后重启会让在途
    会话蒸发、``up/<uid>`` 孤儿对象无从回收。落表后确认与清扫都不再依赖 Redis 存活。
    """
    if settings.storage_backend != "s3":
        return UploadInitResp(mode="sync")
    uid = uuid.uuid4().hex
    key = f"up/{uid}"
    storage = _get_storage()
    url = storage.presign_upload(key, expires=_PRESIGN_EXPIRES)
    # tags 以 JSON 数组形态落 meta（与 confirm 侧解析口径一致）；created_at 供清扫判龄。
    meta = json.dumps(
        {
            "key": key,
            "uploader_id": str(cur.id),
            "original_name": info.original_name,
            "mime_type": info.mime_type,
            "category_id": info.category_id,
            "description": info.description,
            "tags": info.tags,
            "classification": info.classification,
            "project_id": str(info.project_id) if info.project_id else None,
            "version_of": str(info.version_of) if info.version_of else None,
            "created_at": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
    )
    await UploadSessionRepository(db).create(
        upload_id=uid,
        uploader_id=cur.id,
        storage_key=key,
        meta=meta,
    )
    return UploadInitResp(mode="direct", upload_id=uid, presigned_url=url)


async def _hash_from_storage(
    storage: StorageBackend, key: str, limit: int
) -> tuple[int, str]:
    """分块读 ``storage.open(key)`` 流式算 SHA3；超 limit 抛 ``FileErr.TOO_LARGE`` 并清理 key。"""
    hasher = hashlib.sha3_256()
    total = 0
    try:
        async for chunk in storage.open(key):
            total += len(chunk)
            if total > limit:
                raise BizError(
                    FileErr.TOO_LARGE,
                    detail=f"Upload exceeds {limit} byte limit",
                )
            hasher.update(chunk)
    except BizError as exc:
        if exc.errcode == FileErr.TOO_LARGE:
            await _safe_delete(storage, key)
        raise
    return total, hasher.hexdigest()


async def _process_storage_upload(
    storage: StorageBackend, key: str, meta: dict[str, Any], size: int
) -> tuple[str, bytes | None]:
    with tempfile.TemporaryFile() as stream:
        async for chunk in storage.open(key):
            await asyncio.to_thread(stream.write, chunk)
        stream.seek(0)
        return await asyncio.to_thread(
            process_upload,
            stream,
            original_name=meta["original_name"],
            description=meta["description"],
            size=size,
        )


async def _register_from_upload(
    db: DbSession,
    meta: dict[str, Any],
    uploader_id: uuid.UUID,
    storage: StorageBackend,
) -> FileInfo:
    """把已直传的随机对象登记为 PENDING 的 LibraryFile（Phase 2-C 可复用核心）。

    无请求上下文的纯函数式登记：显式接收 ``uploader_id``（事件回调里没有 user 上下文），
    后续队列 worker 可直接调用。流程：读随机 key→SHA3→copy/dedup 到内容寻址 key→
    建行（PENDING, uploader_id, ref_count, storage_path 按 backend 对齐 create_file）→
    同步 ref_count→删随机 key。登记失败时保留随机对象供重试。
    """
    key = meta["key"]
    if not await storage.exists(key):
        raise BizError(FileErr.UPLOAD_NOT_FOUND, detail="Uploaded object not found")
    total, content_hash = await _hash_from_storage(
        storage, key, settings.max_upload_bytes
    )
    extracted_text, preview_pdf = await _process_storage_upload(
        storage, key, meta, total
    )
    hash_key = _build_bucket_key(content_hash)
    repo = LibraryFileRepository(db)
    info = FileCreate(
        original_name=meta["original_name"],
        mime_type=meta["mime_type"],
        category_id=meta["category_id"],
        description=meta["description"],
        tags=meta["tags"],
        classification=meta.get("classification", "public"),
        project_id=meta.get("project_id"),
        version_of=meta.get("version_of"),
    )
    code, version, root_id, classification, project_id = await _identity_for_upload(
        db, repo, uploader_id, info
    )
    # 事务锁覆盖拷贝、登记及提交，防并发末引用删除清空复用对象。
    await repo.lock_hash(content_hash)
    if not await storage.exists(hash_key):
        await storage.copy(key, hash_key)
    if preview_pdf is not None:
        try:
            await storage.save(
                io.BytesIO(preview_pdf),
                max_bytes=settings.files_preview_max_bytes,
                bucket_key=f"{hash_key}.preview.pdf",
            )
        except BizError:
            logger.warning(
                "直传文件 PDF 预览保存失败 hash=%s", content_hash, exc_info=True
            )
    storage_path = _storage_path_for(content_hash)
    # 登记 PENDING（tags 标记里是 JSON 数组，转回 JSON 字符串存储，与 create_file 一致）
    f = LibraryFile(
        uploader_id=uploader_id,
        original_name=meta["original_name"],
        stored_name=_make_stored_name(meta["original_name"]),
        sha3_hash=content_hash,
        ref_count=1,
        storage_path=storage_path,
        mime_type=meta["mime_type"],
        size=total,
        category_id=info.category_id,
        document_code=code,
        version=version,
        root_file_id=root_id,
        classification=classification,
        project_id=project_id,
        extracted_text=extracted_text,
        description=info.description,
        tags=json.dumps(info.tags, ensure_ascii=False),
        status=FileStatus.PENDING,
    )
    await repo.add(f)
    await repo.flush()
    if root_id is None:
        f.root_file_id = f.id
    await repo.sync_ref_count(content_hash)
    names = await _uploader_map(db, [uploader_id])
    result = _file_to_schema(f, names.get(uploader_id, ""))
    await _safe_delete(storage, key)
    return result


async def confirm_upload(db: DbSession, upload_id: str, cur: CurrentUser) -> FileInfo:
    """确认预签名直传：回读对象→SHA3→去重/copy 到内容寻址 key→登记 PENDING。

    认领 ``upload_sessions`` 中的一行（删行即原子消费，幂等：同 upload_id 仅可确认一次）。
    会话缺失/已用 → ``UPLOAD_EXPIRED``；随机 key 对象不存在 → ``UPLOAD_NOT_FOUND``。

    薄封装：认领→解析 meta→调用 ``_register_from_upload``（登记核心已抽出复用）。
    """
    session = await UploadSessionRepository(db).claim(upload_id, uploader_id=cur.id)
    if session is None:
        raise BizError(FileErr.UPLOAD_EXPIRED, detail="Upload session expired/used")
    try:
        meta = json.loads(session.meta)
    except json.JSONDecodeError:
        raise BizError(FileErr.UPLOAD_EXPIRED) from None
    storage = _get_storage()
    return await _register_from_upload(db, meta, session.uploader_id, storage)


async def _safe_delete(storage: StorageBackend, key: str) -> None:
    # 尽力清理失败对象；key 已不存在视为成功
    with suppress(BizError):
        await storage.delete(key)
