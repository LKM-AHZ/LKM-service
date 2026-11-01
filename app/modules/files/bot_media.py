"""bot 产出媒体的入库、公开取用与清理，以及社区文件库的内部只读面。

调用方是 LKM-bot（服务身份，见 ``core.internal_auth``），不是浏览器。两条链路：

1. **写**：bot 把 TTS 音频、T2I 图片、聊天附件 POST 上来 → 内容寻址落进 ``storage`` 后端
   （Local/S3 同一套），登记 ``bot_media_objects``，回一个**过期签名 URL**。
2. **读**：bot 需要看社区文件库里的文件时，走 ``list_library_files`` / ``library_content``，
   只读已审阅通过（APPROVED）的条目。

为什么媒体 URL 要签名而不是直接公开：发给 QQ / Telegram 的媒体链接必须**无鉴权可拉取**
（IM 平台不会带我们的会话），但也不能是永久公开地址。故 URL 里带 ``exp`` + HMAC 签名，
过期即失效；因此不依赖 S3 预签名（Local 后端没有预签名，两套后端会走出两套语义）。

签名密钥不新增必填环境变量：从 ``bot_internal_token`` 做域分离派生（``lkm:media-url`` 前缀）。
这样「内部服务面关闭」（令牌未配置）时，公开媒体面也随之无法铸造 URL——不会出现
「内部关了但公网还能读」的错位。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.modules.files.errors import FileErr
from app.modules.files.models import (
    BotMediaObject,
    BotMediaSource,
    FileStatus,
    LibraryFile,
)
from app.modules.files.service import _buffer_and_hash
from core.config import settings
from core.db.repository import DbSession
from core.err import BizError
from core.secrets import reveal
from core.storage.errors import StorageErr
from core.storage.factory import get_storage_for_settings as _get_storage

logger = logging.getLogger(__name__)

#: 对象 key 前缀：与文件库的 ``<hash[:2]>/<hash>`` 隔开。内容相同则哈希相同，若共用前缀，
#: 删一侧的 blob 会连另一侧一起打穿。
BOT_MEDIA_PREFIX = "bot-media"

#: 公开媒体 URL 的默认有效期（秒）。IM 平台通常在收到消息后数秒内拉取，10 分钟足够。
_DEFAULT_URL_TTL = 900

#: 流式读取块大小（与文件库服务一致）。
_CHUNK = 1024 * 256

_SOURCE_VALUES = frozenset(
    {
        BotMediaSource.TTS,
        BotMediaSource.T2I,
        BotMediaSource.ATTACHMENT,
        BotMediaSource.OTHER,
    }
)


@dataclass(slots=True)
class BotMediaInfo:
    """一次媒体登记的对外视图。``url`` 是可直接发给 IM 平台的签名地址（相对网关根路径）。"""

    id: uuid.UUID
    original_name: str
    mime_type: str
    size: int
    source: str
    sha3_hash: str
    url: str
    expires_in: int


def _bucket_key(content_hash: str) -> str:
    """内容寻址逻辑 key：``bot-media/<hash[:2]>/<hash>``（Local/S3 同形）。"""
    return f"{BOT_MEDIA_PREFIX}/{content_hash[:2]}/{content_hash}"


def url_ttl() -> int:
    """签名 URL 有效期（秒）；0 或非法值回落到默认值，避免签出「立即过期」的链接。"""
    configured = int(settings.bot_media_url_ttl_s or 0)
    return configured if configured > 0 else _DEFAULT_URL_TTL


def _signing_key() -> bytes:
    """公开媒体签名密钥：由内部服务令牌派生（域分离），不额外引入必填环境变量。"""
    seed = reveal(settings.bot_internal_token).strip()
    if not seed:
        raise BizError(FileErr.NOT_FOUND, detail="bot service is not configured")
    return hmac.new(seed.encode("utf-8"), b"lkm:media-url", hashlib.sha256).digest()


def _signature(media_id: uuid.UUID, exp: int) -> str:
    """``HMAC-SHA256("<id>:<exp>")`` 的十六进制串。"""
    msg = f"{media_id}:{exp}".encode()
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()


def public_url(media_id: uuid.UUID) -> str:
    """构造公开媒体 URL（相对网关根路径，由 bot 侧补公共 origin）。"""
    exp = int(time.time()) + url_ttl()
    sig = _signature(media_id, exp)
    return f"/api/v1/media/{media_id}?exp={exp}&sig={sig}"


def verify_public_token(media_id: uuid.UUID, exp: int, sig: str) -> bool:
    """校验公开媒体 URL 的签名与有效期。

    Returns:
        合法且未过期 → True；签名不匹配或已过期 → False。
    """
    if exp <= int(time.time()):
        return False
    try:
        expected = _signature(media_id, exp)
    except BizError:
        return False
    return hmac.compare_digest(expected, sig)


def _info(row: BotMediaObject) -> BotMediaInfo:
    return BotMediaInfo(
        id=row.id,
        original_name=row.original_name,
        mime_type=row.mime_type,
        size=row.size,
        source=row.source,
        sha3_hash=row.sha3_hash,
        url=public_url(row.id),
        expires_in=url_ttl(),
    )


def _expiry(ttl_hours: int | None) -> datetime.datetime | None:
    """计算保留期截止时间；``<=0`` 返回 None（不自动过期）。"""
    hours = settings.bot_media_ttl_hours if ttl_hours is None else ttl_hours
    if hours <= 0:
        return None
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours)


async def _find_by_hash(db: DbSession, content_hash: str) -> BotMediaObject | None:
    res = await db.execute(
        select(BotMediaObject).where(BotMediaObject.sha3_hash == content_hash)
    )
    return res.scalar_one_or_none()


async def create_media(
    db: DbSession,
    stream: object,
    *,
    mime_type: str,
    original_name: str,
    source: str = BotMediaSource.OTHER,
    ttl_hours: int | None = None,
) -> BotMediaInfo:
    """把一条媒体写入对象存储并登记，返回带签名 URL 的视图。

    去重与文件库一致：先算 SHA3-256，命中已有行只递增 ``ref_count`` 并刷新保留期，不重复写 blob。

    Args:
        db: 请求内会话（由依赖负责 commit）。
        stream: 提供同步 ``read(n)`` 的流（``UploadFile.file``）。
        mime_type: 内容类型；空值归一为 ``application/octet-stream``。
        original_name: 原始文件名，仅用于展示与后缀推断。
        source: ``BotMediaSource`` 取值之一；未知值归为 ``other``。
        ttl_hours: 保留小时数；None 取配置，``<=0`` 表示不自动过期。

    Returns:
        登记结果（含公开 URL）。

    Raises:
        BizError: 超限（413）或存储失败（500）。
    """
    limit = settings.bot_media_max_bytes
    total, content_hash, buf = await asyncio.to_thread(_buffer_and_hash, stream, limit)
    try:
        existing = await _find_by_hash(db, content_hash)
        if existing is not None:
            existing.ref_count += 1
            existing.expires_at = _expiry(ttl_hours)
            await db.flush()
            return _info(existing)

        bucket_key = _bucket_key(content_hash)
        storage = _get_storage()
        saved_path = ""
        try:
            if not await storage.exists(bucket_key):
                saved = await storage.save(buf, max_bytes=limit, bucket_key=bucket_key)
                saved_path = str(saved["storage_path"])
        except BizError as exc:
            # 与文件库同一映射：StorageErr→FileErr，调用方契约不变
            if exc.errcode == StorageErr.TOO_LARGE:
                raise BizError(FileErr.TOO_LARGE, detail=exc.detail) from exc
            raise BizError(FileErr.STORE_ERROR, detail=exc.detail) from exc

        row = BotMediaObject(
            sha3_hash=content_hash,
            bucket_key=bucket_key,
            storage_path=saved_path,
            original_name=(original_name or "")[:255],
            mime_type=(mime_type or "application/octet-stream")[:100],
            size=total,
            source=source if source in _SOURCE_VALUES else BotMediaSource.OTHER,
            ref_count=1,
            expires_at=_expiry(ttl_hours),
        )
        db.add(row)
        await db.flush()
        return _info(row)
    finally:
        buf.close()


async def get_media(db: DbSession, media_id: uuid.UUID) -> BotMediaObject:
    """按 id 取登记行；不存在抛 404。"""
    row = await db.get(BotMediaObject, media_id)
    if row is None:
        raise BizError(FileErr.NOT_FOUND, detail="Media object not found")
    return row


async def get_media_url(db: DbSession, media_id: uuid.UUID) -> BotMediaInfo:
    """重新签发一条媒体的公开 URL（原 URL 过期后 bot 可再取）。"""
    return _info(await get_media(db, media_id))


async def _iter_object(key: str) -> AsyncIterator[bytes]:
    """逐块读对象。响应头已发出后无法再改状态码，故存在性由调用方预检。"""
    source = _get_storage().open(key)
    async for chunk in source:
        yield chunk


async def serve_media(
    db: DbSession, media_id: uuid.UUID, *, download: bool = False
) -> StreamingResponse:
    """公开媒体取用（签名 URL 命中后由路由层无鉴权调用）：流式返回对象。

    预检对象存在性：响应头一旦发出，生成器里的存储错误无法再变成 404（客户端会收到 200 +
    截断体）。

    Args:
        db: 会话。
        media_id: 媒体 id。
        download: True 时以附件形式下载（``Content-Disposition: attachment``）。

    Returns:
        流式响应；``Cache-Control`` 允许 IM/CDN 侧短暂缓存。
    """
    row = await get_media(db, media_id)
    if row.expires_at is not None and row.expires_at < datetime.datetime.now(
        datetime.UTC
    ):
        raise BizError(FileErr.NOT_FOUND, detail="Media object expired")
    if not await _get_storage().exists(row.bucket_key):
        raise BizError(FileErr.NOT_FOUND, detail="Stored object not found")
    # 生成器只读 storage，不再需要 DB：提前结束事务，把连接还给池（慢客户端不再长期占连）
    await db.commit()
    headers = {
        "Cache-Control": f"public, max-age={max(1, min(url_ttl(), 300))}",
        "X-Content-Type-Options": "nosniff",
    }
    if download:
        safe = Path(row.original_name).name or "download"
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return StreamingResponse(
        _iter_object(row.bucket_key),
        media_type=row.mime_type or "application/octet-stream",
        headers=headers,
    )


async def delete_media(db: DbSession, media_id: uuid.UUID) -> None:
    """删除登记；引用计数归零时才删物理对象（同内容可能被多条消息共享）。"""
    row = await get_media(db, media_id)
    row.ref_count -= 1
    if row.ref_count > 0:
        await db.flush()
        return
    key = row.bucket_key
    await db.delete(row)
    await db.flush()
    try:
        await _get_storage().delete(key)
    except BizError:
        # 物理删除失败不让登记回滚：留下孤儿对象比「记录没了但对象还在」更难追。告警即可。
        logger.warning("bot media object delete failed key=%s", key, exc_info=True)


async def purge_expired(db: DbSession, *, limit: int = 200) -> int:
    """清理已过保留期的媒体（供运维任务调用），返回处理条数。

    Args:
        db: 会话。
        limit: 单次最多处理条数（避免一次拖全表）。

    Returns:
        处理的登记条数。
    """
    now = datetime.datetime.now(datetime.UTC)
    res = await db.execute(
        select(BotMediaObject)
        .where(BotMediaObject.expires_at.is_not(None), BotMediaObject.expires_at < now)
        .limit(limit)
    )
    processed = 0
    for row in res.scalars().all():
        await delete_media(db, row.id)
        processed += 1
    return processed


async def stats(db: DbSession) -> dict[str, int]:
    """媒体统计（供内部状态端点）：条数与总字节。"""
    res = await db.execute(
        select(
            func.count(BotMediaObject.id),
            func.coalesce(func.sum(BotMediaObject.size), 0),
        )
    )
    count, total = res.one()
    return {"objects": int(count or 0), "bytes": int(total or 0)}


# ---- 社区文件库内部只读面（bot 读取后端文件） ----


async def list_library_files(
    db: DbSession, *, page: int = 1, size: int = 20, keyword: str = ""
) -> tuple[list[dict[str, object]], int]:
    """列出**已审阅通过**的文件库条目（bot 只读，不暴露待审/已拒绝内容）。

    Args:
        db: 会话。
        page: 1 基页码。
        size: 每页条数（调用方已裁剪上限）。
        keyword: 可选模糊匹配（文件名或正文）。

    Returns:
        ``(items, total)``；items 只含 bot 需要的字段。
    """
    base = select(LibraryFile).where(LibraryFile.status == FileStatus.APPROVED)
    count_stmt = select(func.count(LibraryFile.id)).where(
        LibraryFile.status == FileStatus.APPROVED
    )
    if keyword:
        pattern = f"%{keyword.strip()}%"
        cond = LibraryFile.original_name.ilike(
            pattern
        ) | LibraryFile.extracted_text.ilike(pattern)
        base = base.where(cond)
        count_stmt = count_stmt.where(cond)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    rows = (
        (
            await db.execute(
                base.order_by(LibraryFile.created_at.desc())
                .offset(max(0, (page - 1) * size))
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    items: list[dict[str, object]] = [
        {
            "id": str(r.id),
            "name": r.original_name,
            "mime_type": r.mime_type,
            "size": r.size,
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "description": r.description,
            "classification": r.classification,
        }
        for r in rows
    ]
    return items, total


async def library_content(db: DbSession, file_id: uuid.UUID) -> StreamingResponse:
    """流式返回一个已通过审阅的文件内容（bot 只读）。"""
    f = await db.get(LibraryFile, file_id)
    if f is None:
        raise BizError(FileErr.NOT_FOUND)
    if f.status != FileStatus.APPROVED:
        raise BizError(FileErr.NOT_APPROVED)
    if not f.sha3_hash:
        raise BizError(FileErr.NOT_FOUND, detail="File has no stored content")
    key = f"{f.sha3_hash[:2]}/{f.sha3_hash}"
    if not await _get_storage().exists(key):
        raise BizError(FileErr.NOT_FOUND, detail="Stored object not found")
    await db.commit()
    return StreamingResponse(
        _iter_object(key),
        media_type=f.mime_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f'attachment; filename="{Path(f.original_name).name}"'
            ),
        },
    )
