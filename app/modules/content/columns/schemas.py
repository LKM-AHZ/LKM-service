import datetime
import json
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.content.column_models import (
    ColumnApplicationStatus,
    ColumnPostStatus,
    ColumnStatus,
)


class ColumnApplicationCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=1, max_length=300)
    reason: str = Field(..., min_length=1, max_length=500)


class ColumnApplicationInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    description: str
    reason: str
    status: ColumnApplicationStatus = ColumnApplicationStatus.PENDING
    reviewer_id: uuid.UUID | None = None
    review_note: str | None = None
    created_at: datetime.datetime
    reviewed_at: datetime.datetime | None = None


class ColumnApplicationReview(BaseModel):
    status: ColumnApplicationStatus
    review_note: str | None = Field(default=None, max_length=300)


class ColumnInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    application_id: uuid.UUID | None = None
    title: str
    description: str
    slug: str | None = None
    cover_url: str | None = None
    author_name: str | None = None
    author_title: str | None = None
    author_bio: str | None = None
    avatar_url: str | None = None
    is_verified: bool = False
    follower_count: int = 0
    like_count: int = 0
    subscribe_count: int = 0
    article_count: int = 0
    tags: list[str] = Field(default_factory=list)
    badges: list[str] = Field(default_factory=list)
    board_id: uuid.UUID | None = None
    status: ColumnStatus = ColumnStatus.ACTIVE
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("tags", "badges", mode="before")
    @classmethod
    def _parse_str_list(cls, v: object) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        if not isinstance(v, str):
            return []
        try:
            parsed = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return []


class ColumnPostCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=300)
    content: str = Field(..., min_length=1, max_length=200_000)


class ColumnPostInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    column_id: uuid.UUID
    author_id: uuid.UUID
    title: str
    summary: str | None = None
    content: str = ""
    cover_image: str | None = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    status: ColumnPostStatus = ColumnPostStatus.DRAFT
    created_at: datetime.datetime
    updated_at: datetime.datetime
    published_at: datetime.datetime | None = None


class ReviewResultData(BaseModel):
    application: ColumnApplicationInfo
    column: ColumnInfo | None = None


class ColumnPlanData(BaseModel):
    status: str
    tables: dict[str, Any]
    next_steps: list[str]
