"""files 列表读响应的 msgspec 镜像（B6c，roadmap §6.5.2 扩面）。

只覆盖**列表**端点（``GET /files``）——单条详情走一次序列化，收益不足门槛。
字段名保持 snake_case（与 ``feed/wire.py`` 同理）。
"""

from __future__ import annotations

import datetime
import uuid

import msgspec

from app.modules.files.schemas import FileInfo
from core.common import PageData


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
    document_code: str | None
    classification: str
    project_id: uuid.UUID | None
    version: int
    root_file_id: uuid.UUID | None
    archive_state: str
    backed_up_at: datetime.datetime | None


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
                document_code=item.document_code,
                classification=item.classification,
                project_id=item.project_id,
                version=item.version,
                root_file_id=item.root_file_id,
                archive_state=item.archive_state,
                backed_up_at=item.backed_up_at,
            )
            for item in page.items
        ],
        total=page.total,
        page=page.page,
        pages=page.pages,
    )
