import datetime
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.auth.schemas import ProfileInfo
from app.modules.blog.models import BlogSeriesStatus

# ---- request schemas ----


class BlogSeriesCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    cover_url: str | None = None
    repo_name: str = Field(..., min_length=1, max_length=100)


class BlogSeriesUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    cover_url: str | None = None
    status: BlogSeriesStatus | None = None


class BlogCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    parent_id: uuid.UUID | None = None


# ---- response schemas ----


class BlogStarStatus(BaseModel):
    starred: bool
    star_count: int


class BlogSeriesInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None = None
    cover_url: str | None = None
    repo_name: str
    status: BlogSeriesStatus = BlogSeriesStatus.ACTIVE
    created_at: datetime.datetime
    updated_at: datetime.datetime
    star_count: int = 0
    is_starred: bool = False


class BlogSeriesDetail(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None = None
    cover_url: str | None = None
    repo_name: str
    status: BlogSeriesStatus = BlogSeriesStatus.ACTIVE
    created_at: datetime.datetime
    updated_at: datetime.datetime
    star_count: int = 0
    is_starred: bool = False
    file_tree: list[dict[str, Any]] | None = None


class BlogCommentInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    series_id: uuid.UUID
    content: str
    parent_id: uuid.UUID | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    profile: ProfileInfo | None = None
    replies: list["BlogCommentInfo"] = []

    @field_validator("replies", mode="before")
    @classmethod
    def _ignore_orm_replies(cls, v: Any) -> list[Any]:
        # 树由 service 手动拼装，忽略 ORM 的 replies 关联
        return []


BlogCommentInfo.model_rebuild()


class SeriesFileWrite(BaseModel):
    content: str
    message: str | None = None


class SeriesPublish(BaseModel):
    filepath: str
    override: dict[str, Any] | None = None
