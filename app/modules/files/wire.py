"""files 列表读响应的 msgspec 镜像（B6c，roadmap §6.5.2 扩面）。

只覆盖**列表**端点（``GET /files``）——单条详情走一次序列化，收益不足门槛。
字段名保持 snake_case（与 ``feed/wire.py`` 同理）。
"""

from __future__ import annotations

import datetime
import uuid

import msgspec

from app.core.common import PageData
from app.modules.files.schemas import FileInfo


class FileInfoWire(msgspec.Struct):
    """``FileInfo`` 的 msgspec 镜像（字段同序同名）。"""

    id: uuid.UUID
    original_name: str
    uploader_id: uuid.UUID
    uploader_name: str
    mime_type: str
    size: int
    category_id: str
    category_name: str
    description: str
    tags: list[str]
    status: str
    review_comment: str | None
    download_count: int
    view_count: int
    created_at: datetime.datetime


class FilePageWire(msgspec.Struct):
    items: list[FileInfoWire]
    total: int
    page: int
    pages: int


def to_wire(page: PageData[FileInfo]) -> FilePageWire:
    return FilePageWire(
        items=[
            FileInfoWire(
                id=item.id,
                original_name=item.original_name,
                uploader_id=item.uploader_id,
                uploader_name=item.uploader_name,
                mime_type=item.mime_type,
                size=item.size,
                category_id=item.category_id,
                category_name=item.category_name,
                description=item.description,
                tags=list(item.tags),
                status=item.status,
                review_comment=item.review_comment,
                download_count=item.download_count,
                view_count=item.view_count,
                created_at=item.created_at,
            )
            for item in page.items
        ],
        total=page.total,
        page=page.page,
        pages=page.pages,
    )
