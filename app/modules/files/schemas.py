import datetime
import uuid
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.common import parse_tags


class FileCreate(BaseModel):
    original_name: str = Field(..., min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=100)
    category_id: str = Field(default="", max_length=50)
    description: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list)


class FileInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    uploader_id: uuid.UUID
    uploader_name: str = ""
    mime_type: str
    size: int
    category_id: str
    category_name: str = ""
    description: str
    tags: list[str]
    status: str
    review_comment: str | None = None
    download_count: int
    view_count: int
    created_at: datetime.datetime

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, v: object) -> list[str]:
        return parse_tags(v)


class UploadInitResp(BaseModel):
    mode: Literal["direct", "sync"]
    upload_id: str | None = None  # direct 时
    presigned_url: str | None = None  # direct 时
    file: FileInfo | None = None  # 预留(当前 sync 前端回退 multipart, 故常 None)


class DownloadUrlInfo(BaseModel):
    """下载 URL 描述对象：frontend 依 kind 分叉下载方式。"""

    kind: Literal["backend", "presigned"]
    url: str
    expires_in: int | None = None
