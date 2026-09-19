import asyncio
import hashlib
import json
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol
from urllib.parse import quote

from fastapi.responses import StreamingResponse

from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.config import settings
from app.core.err import BizError
from app.core.redis import get_redis
from app.core.secrets import reveal
from app.db.repo import get_or_raise
from app.db.repository import DbSession
from app.modules.files.errors import FileErr
from app.modules.files.models import FILES_TABLE_PLAN, FileStatus, LibraryFile
from app.modules.files.repository import LibraryFileRepository
from app.modules.files.schemas import (
    DownloadUrlInfo,
    FileCreate,
    FileInfo,
    UploadInitResp,
)
from app.modules.points.rules import enqueue_points_event
from app.modules.storage.base import StorageBackend
from app.modules.storage.errors import StorageErr
from app.modules.storage.factory import get_storage
from auth.deps import CurrentUser
from auth.snapshot import get_user_snapshot_batch


class _Readable(Protocol):
    """可同步分块读取的 file-like 对象最小协议。"""

    def read(self, size: int = -1, /) -> bytes: ...


class _Spool(_Readable, Protocol):
    """``_buffer_and_hash`` 返回的 spool 流：除 read 外还需能 seek 与 close（释放临时文件）。"""

    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def close(self) -> None: ...


def get_files_plan() -> dict[str, Any]:
    return {
        "status": "implemented_minimal",
        "tables": FILES_TABLE_PLAN,
        "next_steps": [
            "Add review approval workflow",
            "Add duplicate / plagiarism detection",
            "Add file serving with presigned URL",
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


async def list_files(
    db: DbSession,
    page: int = 1,
    limit: int = 20,
    category_id: str | None = None,
    status: str | None = None,
    sort: str = "newest",
) -> PageData[FileInfo]:
    repo = LibraryFileRepository(db)
    total = await repo.count_page(category_id=category_id, status=status)
    files = await repo.list_page(
        category_id=category_id,
        status=status,
        sort=sort,
        offset=paginate_offset(page, limit),
        limit=limit,
    )

    names = await _uploader_map(db, [f.uploader_id for f in files])
    items = [_file_to_schema(f, names.get(f.uploader_id, "")) for f in files]
    return PageData(
        items=items, total=total, page=page, pages=paginate_pages(total, limit)
    )


async def get_file(
    db: DbSession, file_id: uuid.UUID, bump_view: bool = False
) -> FileInfo:
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )

    if bump_view:
        f.view_count += 1
        await LibraryFileRepository(db).flush()

    names = await _uploader_map(db, [f.uploader_id])
    return _file_to_schema(f, names.get(f.uploader_id, ""))


_CHUNK = 1024 * 1024  # 分块读写，避免整文件载入内存


def _buffer_and_hash(stream: _Readable, limit: int) -> tuple[int, str, _Spool]:
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


# ---- 内容哈希级互斥（串行化「去重复用」与「末引用物理删除」，防 TOCTOU）----
# delete_file 在"引用归零"时物理删 blob，create_file 可能在删除的前后复用同一 blob。
# 二者对同一 content_hash 的决策必须互斥，否则出现空引用/孤儿 blob。Redis 有则用
# 分布式锁（跨 worker 生效），无则退回进程内锁（单 worker / 测试语义仍正确）。
_HASH_LOCK_TTL_SECONDS = 30
_hash_locks_inproc: dict[str, asyncio.Lock] = {}


async def _acquire_hash_lock(content_hash: str) -> bool:
    """尝试获取 content_hash 级互斥锁；拿到返回 True（调用方必须 __release_hash_lock）。"""
    redis = await get_redis()
    if redis is None:
        lock = _hash_locks_inproc.setdefault(content_hash, asyncio.Lock())
        return await lock.acquire()
    got = await redis.set(
        f"files:hash:{content_hash}", "1", ex=_HASH_LOCK_TTL_SECONDS, nx=True
    )
    return bool(got)


async def _release_hash_lock(content_hash: str) -> None:
    redis = await get_redis()
    if redis is None:
        lock = _hash_locks_inproc.get(content_hash)
        if lock is not None:
            lock.release()
        return
    await redis.delete(f"files:hash:{content_hash}")


@asynccontextmanager
async def _hash_lock(content_hash: str) -> AsyncIterator[None]:
    """await 获取锁，确保拿到后在退出时释放。锁等不到/Redis 异常按放行(不阻断上传/删除)。"""
    try:
        acquired = await _acquire_hash_lock(content_hash)
    except Exception:
        acquired = False
    # 拿不到锁（并发争抢或 Redis 抖动）→ 重验仍可能竞争，但返回 None 不额外报错；
    # 为不放大风险，拿不到时也照常放行（原语义），锁主要串行化常规并发窗口。
    try:
        yield
    finally:
        if acquired:
            with suppress(Exception):
                await _release_hash_lock(content_hash)


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
    limit = max_bytes or settings.max_upload_bytes
    total, content_hash, buf = _buffer_and_hash(stream, limit)
    bucket_key = _build_bucket_key(content_hash)

    # 落盘（写字节的细节交给 storage 层）；dedup 语义：已存在则复用、不重写。
    # 与 delete_file 的末引用物理删除互斥（按 content_hash 加锁），避免并发删除
    # 恰在 exists→save 间隙把复用的 blob 删空造成空引用。
    saved: dict[str, object] | None = None
    try:
        async with _hash_lock(content_hash):
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

    if saved is not None:
        storage_path = str(saved["storage_path"])
    else:
        # 复用既有物理文件：storage_path 须与首写时后端实际返回的一致，保证元数据不漂移。
        storage_path = _storage_path_for(content_hash)

    repo = LibraryFileRepository(db)
    try:
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
            description=info.description,
            tags=json.dumps(info.tags, ensure_ascii=False),
        )
        await repo.add(f)
        await repo.sync_ref_count(content_hash)
        await repo.flush()
    except Exception:
        # 入库失败：仅当物理文件在本次是唯一引用（无其他条目）时才回收磁盘。
        if await repo.count_by_hash(content_hash) <= 1:
            with suppress(BizError, OSError):  # 尽力清理，不覆盖原始入库异常
                await _get_storage().delete(bucket_key)
        raise

    names = await _uploader_map(db, [f.uploader_id])
    return _file_to_schema(f, names.get(f.uploader_id, ""))


async def bump_download(db: DbSession, file_id: uuid.UUID) -> int:
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    f.download_count += 1
    await LibraryFileRepository(db).flush()
    return f.download_count


async def review_file(
    db: DbSession,
    file_id: uuid.UUID,
    target_status: FileStatus,
    review_comment: str | None = None,
    is_admin: bool = False,
) -> FileInfo:
    """管理员审核文件：通过 / 驳回（驳回时删除物理文件并联动同 hash 条目置 REJECTED）。"""
    if not is_admin:
        raise BizError(FileErr.STORE_ERROR, detail="Only admin can review files")
    if target_status not in (FileStatus.APPROVED, FileStatus.REJECTED):
        raise BizError(FileErr.INVALID_STATUS, detail="Invalid review status")

    repo = LibraryFileRepository(db)
    # 行锁读取：两个管理员并发审核同一 PENDING 文件时，串行化「读 PENDING → 改 status」
    # 的读改写，避免都通过 PENDING 检查后 last-write-wins 造成状态/加分不确定。
    f = await repo.get_locked(file_id)
    if f is None:
        raise BizError(FileErr.NOT_FOUND, detail="File not found")
    if f.status != FileStatus.PENDING:
        raise BizError(FileErr.NOT_PENDING, detail="File is not pending")

    f.status = target_status
    f.review_comment = review_comment

    if target_status == FileStatus.REJECTED and f.sha3_hash:
        # 同一物理文件被多个条目引用：一并标记 REJECTED，并删除物理文件。
        for other in await repo.list_by_hash(f.sha3_hash):
            other.status = FileStatus.REJECTED
            other.review_comment = other.review_comment or review_comment
        await repo.flush()
        # 删除物理文件（尽力而为：key 已不存在视为成功，保持原来的 missing_ok 语义）。
        bucket_key = _bucket_key_of(f)
        if bucket_key is not None:
            try:
                await _get_storage().delete(bucket_key)
            except BizError as exc:
                if exc.errcode != StorageErr.NOT_FOUND:
                    _raise_storage_as_file(exc)
    else:
        await repo.flush()

    # 仅审核通过时给归属者加分（f.status 已设为 target_status）
    if f.status == FileStatus.APPROVED:
        await enqueue_points_event(db, f.uploader_id, "file_approved", f"file:{f.id}")

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
    if not is_admin and f.uploader_id != actor_id:
        raise BizError(FileErr.STORE_ERROR, detail="Not the owner of this file")

    old_hash = f.sha3_hash
    f.status = FileStatus.DELETED
    await repo.flush()

    if old_hash:
        # 物理删除决策与 create_file 的去重复用互斥（按 content_hash 加锁）：
        # 加锁后重算引用，防止并发创建/删除的 TOCTOU 把仍被引用的 blob 删空。
        async with _hash_lock(old_hash):
            remaining = await repo.count_live_by_hash(old_hash)
            await repo.sync_ref_count(old_hash)
            # 加锁后仍无引用才物理删除（key 已不存在视为成功）。
            if remaining <= 0:
                bucket_key = _build_bucket_key(old_hash)
                try:
                    await _get_storage().delete(bucket_key)
                except BizError as exc:
                    if exc.errcode != StorageErr.NOT_FOUND:
                        _raise_storage_as_file(exc)

    names = await _uploader_map(db, [f.uploader_id])
    return _file_to_schema(f, names.get(f.uploader_id, ""))


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
    _require_approved(f, action="download")
    key = _bucket_key_of(f)
    if key is None:
        raise BizError(FileErr.NOT_FOUND, detail="File has no stored content")
    f.download_count += 1
    await LibraryFileRepository(db).flush()
    storage = _get_storage()
    if settings.storage_backend == "s3":
        url = storage.presign_download(key, expires=60)
        return DownloadUrlInfo(kind="presigned", url=url, expires_in=60)
    return DownloadUrlInfo(kind="backend", url=f"/api/v1/files/{file_id}/content")


def _serve(
    db: DbSession, f: LibraryFile, disposition: Literal["inline", "attachment"]
) -> StreamingResponse:
    """构造流式响应：逐块读取 storage 字节，不整载内存；存储错误映射为 FileErr。"""

    async def it() -> AsyncIterator[bytes]:
        try:
            async for chunk in _get_storage().open(_bucket_key_of(f) or ""):
                yield chunk
        except BizError as exc:
            _raise_storage_as_file(exc)

    # 头只能含 latin-1 可编码字节，中文等非 ASCII 文件名按 RFC 5987 filename* 编码，
    # 同时给一个 ASCII 化的 filename 兜底，保证旧客户端也能识别。
    ascii_fallback = (
        f.original_name.encode("ascii", "ignore").decode("ascii") or "download"
    )
    cd = (
        f"{disposition}; filename={ascii_fallback}; "
        f"filename*=UTF-8''{quote(f.original_name)}"
    )
    headers = {
        "Content-Disposition": cd,
        # 文件端点需登录私有：禁 public immutable，避免未经授权的内容被缓存/跨代理复用
        "Cache-Control": "private, no-store",
    }
    return StreamingResponse(it(), media_type=f.mime_type, headers=headers)


async def serve_content(
    db: DbSession,
    file_id: uuid.UUID,
    disposition: Literal["inline", "attachment"],
) -> StreamingResponse:
    """预览(/preview)/下载(/content)共用入口：仅 APPROVED 可访问；预览计次 view_count。"""
    f = await get_or_raise(
        db, LibraryFile, FileErr.NOT_FOUND, LibraryFile.id == file_id
    )
    _require_approved(f, action="preview" if disposition == "inline" else "download")
    if disposition == "inline":  # 预览计次 view
        f.view_count += 1
        await LibraryFileRepository(db).flush()
    return _serve(db, f, disposition)


# ---- Phase 2-B: 预签名直传（upload-init / confirm，Redis 标记 + 回读哈希去重） ----

_UPLOAD_TTL = 3600  # 标记"年龄窗口"1h：清扫按 created_at 年龄判断（标记本身持久化）
_PRESIGN_EXPIRES = 900  # presigned PUT 15min
_UPLOAD_PREFIX = "upload:"


def _upload_key(upload_id: str) -> str:
    return f"{_UPLOAD_PREFIX}{upload_id}"


async def upload_init(
    db: DbSession, info: FileCreate, cur: CurrentUser
) -> UploadInitResp:
    """预签名直传初始化。

    Local→``mode=sync``（前端回退 multipart POST /files，无 upload_id/URL）；
    S3→``mode=direct``，生成独立随机 key（``up/<uuid>``）+ 预签名 PUT URL，并把元数据
    随 Redis 标记存下（供 confirm 登记用）。Redis 不可用则只返回 URL（不落标记，
    confirm 会因拿不到标记而失败——fail-open 只作用于限流，这里显式 410 语义）。
    """
    if settings.storage_backend != "s3":
        return UploadInitResp(mode="sync")
    uid = uuid.uuid4().hex
    key = f"up/{uid}"
    storage = _get_storage()
    url = storage.presign_upload(key, expires=_PRESIGN_EXPIRES)
    redis = await get_redis()
    if redis is not None:
        # 元数据随标记存，供 confirm 登记 LibraryFile 用（tags 以 JSON 数组形态落标记）。
        # 标记持久化（不带 ex/ttl）：Redis 会随 TTL 到期自动删除标记，导致清扫 scan 永远看不到
        # "已过期"的标记、up/<uid> 孤儿无法回收（R1）。改由 created_at 记录写入时刻，孤儿清扫
        # 按年龄(_UPLOAD_TTL 窗口)判断是否过期。
        meta = json.dumps(
            {
                "key": key,
                "uploader_id": str(cur.id),
                "original_name": info.original_name,
                "mime_type": info.mime_type,
                "category_id": info.category_id,
                "description": info.description,
                "tags": info.tags,
                "created_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        )
        await redis.set(_upload_key(uid), meta)
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
    except BizError:
        await _safe_delete(storage, key)
        raise
    return total, hasher.hexdigest()


async def _register_from_upload(
    db: DbSession,
    meta: dict[str, Any],
    uploader_id: uuid.UUID,
    storage: StorageBackend,
) -> FileInfo:
    """把已直传的随机对象登记为 PENDING 的 LibraryFile（Phase 2-C 可复用核心）。

    无请求上下文的纯函数式登记：显式接收 ``uploader_id``（事件回调里没有 user 上下文），
    后续队列 worker 可直接调用。流程：读随机 key→SHA3→copy/dedup 到内容寻址 key→删随机
    key→建行（PENDING, uploader_id, ref_count, storage_path 按 backend 对齐 create_file）→
    同步 ref_count。哈希/去重/拷贝逻辑与 ``confirm_upload`` 保持一致，未重写。
    """
    key = meta["key"]
    if not await storage.exists(key):
        raise BizError(FileErr.UPLOAD_NOT_FOUND, detail="Uploaded object not found")
    total, content_hash = await _hash_from_storage(
        storage, key, settings.max_upload_bytes
    )
    hash_key = _build_bucket_key(content_hash)
    # 与 delete_file 的末引用物理删除互斥，防并发删除在 copy→登记间隙清空复用对象。
    async with _hash_lock(content_hash):
        if not await storage.exists(hash_key):
            try:
                await storage.copy(key, hash_key)
            except Exception:
                # copy 失败：随机 up/<uid> 对象尚未删除，尽力回收，覆盖原始异常
                await _safe_delete(storage, key)
                raise
        await _safe_delete(storage, key)
    # storage_path 按 backend 与 create_file 对齐：直传与普通上传的条目不可区分
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
        category_id=meta["category_id"],
        description=meta["description"],
        tags=meta["tags"]
        if isinstance(meta["tags"], str)
        else json.dumps(meta["tags"], ensure_ascii=False),
        status=FileStatus.PENDING,
    )
    repo = LibraryFileRepository(db)
    try:
        await repo.add(f)
        await repo.sync_ref_count(content_hash)
        await repo.flush()
    except Exception:
        # 入库失败且本次 row 未插入成功（count_by_hash 看不到它）：仅当 hash_key 在本次是
        # 唯一引用（<=1）时才回收磁盘，避免误删其他条目共享的物理文件。与 create_file 一致。
        if await repo.count_by_hash(content_hash) <= 1:
            with suppress(BizError, OSError):  # 尽力清理，不覆盖原始入库异常
                await _get_storage().delete(hash_key)
        raise
    names = await _uploader_map(db, [uploader_id])
    return _file_to_schema(f, names.get(uploader_id, ""))


async def confirm_upload(db: DbSession, upload_id: str, cur: CurrentUser) -> FileInfo:
    """确认预签名直传：回读对象→SHA3→去重/copy 到内容寻址 key→登记 PENDING。

    Redis GETDEL 标记（原子 + 幂等：同 upload_id 仅可确认一次）。标记缺失/已用/Redis
    未启用 → ``UPLOAD_EXPIRED``；随机 key 对象不存在 → ``UPLOAD_NOT_FOUND``。

    薄封装：读标记→解析 meta→调用 ``_register_from_upload``（登记核心已抽出复用）。
    """
    redis = await get_redis()
    meta_raw = None
    if redis is not None:
        meta_raw = await redis.getdel(_upload_key(upload_id))
    if not meta_raw:
        raise BizError(FileErr.UPLOAD_EXPIRED, detail="Upload session expired/used")
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError:
        raise BizError(FileErr.UPLOAD_EXPIRED) from None
    storage = _get_storage()
    return await _register_from_upload(
        db, meta, uuid.UUID(meta["uploader_id"]), storage
    )


async def _safe_delete(storage: StorageBackend, key: str) -> None:
    # 尽力清理失败对象；key 已不存在视为成功
    with suppress(BizError):
        await storage.delete(key)
