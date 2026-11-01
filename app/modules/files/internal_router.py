"""bot → backend 内部文件面（服务身份，不走社区用户鉴权）。

全部端点挂在 ``/api/v1/internal/files`` 下，由 ``require_bot_service`` 统一守卫：未配置令牌 /
令牌不符一律 401，故该面不会退化成公网可写存储桶。

两个方向：

* **写**（``POST`` / ``GET /{id}`` / ``DELETE``）：bot 产出媒体的入库、重签 URL、删除。
* **读**（``/library``）：bot 只读社区文件库中**已审阅通过**的条目与内容。

为什么不复用 ``/api/v1/files``：那些端点的权限模型是 ``CurrentUser`` + RBAC 能力点，语义是
「某个社区用户能对自己的文件做什么」。bot 没有社区身份，硬塞一个用户身份会让审计记录说谎，
也会让权限判定变成「反正都过了」。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files import bot_media
from app.modules.files.models import BotMediaSource
from core.common import ApiResp
from core.db.session import get_read_session, get_session
from core.err import respond
from core.internal_auth import require_bot_service

router = APIRouter(
    prefix="/internal/files",
    tags=["internal-files"],
    dependencies=[Depends(require_bot_service)],
)


class BotMediaOut(BaseModel):
    """媒体登记视图（写/重签端点共用）。``url`` 为相对网关根路径的签名地址。"""

    id: str
    original_name: str
    mime_type: str
    size: int
    source: str
    sha3_hash: str
    url: str
    expires_in: int


class BotMediaStatusOut(BaseModel):
    """内部面状态：便于 bot 自检「后端是否已接入」。"""

    objects: int
    bytes: int
    max_bytes: int
    ttl_hours: int


class LibraryPageOut(BaseModel):
    """文件库分页（bot 只读）。"""

    items: list[dict[str, Any]]
    total: int
    page: int
    size: int


def _to_out(info: bot_media.BotMediaInfo) -> BotMediaOut:
    return BotMediaOut(
        id=str(info.id),
        original_name=info.original_name,
        mime_type=info.mime_type,
        size=info.size,
        source=str(info.source),
        sha3_hash=info.sha3_hash,
        url=info.url,
        expires_in=info.expires_in,
    )


@router.get("/status", response_model=ApiResp[BotMediaStatusOut])
@respond
async def media_status(
    db: AsyncSession = Depends(get_read_session),
) -> BotMediaStatusOut:
    """内部面状态与体量统计。"""
    from core.config import settings

    data = await bot_media.stats(db)
    return BotMediaStatusOut(
        objects=data["objects"],
        bytes=data["bytes"],
        max_bytes=settings.bot_media_max_bytes,
        ttl_hours=settings.bot_media_ttl_hours,
    )


@router.post("", response_model=ApiResp[BotMediaOut])
@respond
async def upload_media(
    file: UploadFile = File(...),
    source: BotMediaSource = Form(default=BotMediaSource.OTHER),
    ttl_hours: int | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> BotMediaOut:
    """写入一条 bot 产出媒体，返回带签名 URL 的登记视图。

    去重按内容哈希：同一张图重复写入只递增引用计数，不重复占存储。
    """
    info = await bot_media.create_media(
        db,
        file.file,
        mime_type=file.content_type or "application/octet-stream",
        original_name=file.filename or "media",
        source=source,
        ttl_hours=ttl_hours,
    )
    return _to_out(info)


@router.get("/library", response_model=ApiResp[LibraryPageOut])
@respond
async def list_library(
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: Annotated[str, Query(max_length=100)] = "",
    db: AsyncSession = Depends(get_read_session),
) -> LibraryPageOut:
    """列出文件库中已审阅通过的条目（bot 只读；不含待审/已拒绝内容）。"""
    items, total = await bot_media.list_library_files(
        db, page=page, size=size, keyword=keyword
    )
    return LibraryPageOut(items=items, total=total, page=page, size=size)


@router.get("/library/{file_id}/content")
async def library_file_content(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """流式返回文件库中一个已通过审阅的文件内容。"""
    return await bot_media.library_content(db, file_id)


# 注意路由顺序：``/library`` 必须先于 ``/{media_id}`` 注册，否则会被 UUID 路径参数吞掉
# （``/library`` 不是合法 UUID，会命中 422 而不是列表端点）。
@router.get("/{media_id}", response_model=ApiResp[BotMediaOut])
@respond
async def get_media(
    media_id: uuid.UUID,
    db: AsyncSession = Depends(get_read_session),
) -> BotMediaOut:
    """按 id 取登记视图（含重新签发的 URL）。"""
    return _to_out(await bot_media.get_media_url(db, media_id))


@router.delete("/{media_id}", response_model=ApiResp[dict[str, bool]])
@respond
async def delete_media(
    media_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """删除一条登记；引用计数归零时才删物理对象。"""
    await bot_media.delete_media(db, media_id)
    return {"deleted": True}
