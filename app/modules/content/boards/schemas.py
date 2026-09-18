import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class BoardCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    title: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    parent_id: uuid.UUID | None = Field(default=None)
    require_certified: bool = False
    daily_post_limit: int = Field(default=0, ge=0)
    is_public: bool = True


class BoardOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    description: str = ""
    parent_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    status: str
    require_certified: bool
    daily_post_limit: int
    is_public: bool
    created_at: datetime.datetime


class BoardUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    parent_id: uuid.UUID | None = Field(default=None)
    require_certified: bool | None = None
    daily_post_limit: int | None = Field(default=None, ge=0)
    is_public: bool | None = None


class BoardApplicationCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1, max_length=300)
    reason: str = Field(..., min_length=1, max_length=500)
    slug: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")


class BoardApplicationOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    applicant_id: uuid.UUID
    title: str
    description: str
    reason: str
    slug: str
    status: str
    review_note: str | None = None
    created_at: datetime.datetime
    reviewed_at: datetime.datetime | None = None


class ReviewBoardApplicationRequest(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


class BanRequest(BaseModel):
    user_id: uuid.UUID
    reason: str = Field(default="", max_length=200)
    hours: int = Field(default=7 * 24, ge=1, le=7 * 24)  # 1 小时到 7 天（小时数）
