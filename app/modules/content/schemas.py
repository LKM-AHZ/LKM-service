import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.content import ContentItemInfo as ContentItemInfo


class ContentItemCreate(BaseModel):
    content_type: str = Field(default="discussion")
    board_id: uuid.UUID = Field(...)
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=200_000)
    summary: str | None = Field(default=None, max_length=300)
    cover: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    # 官方发布字段（content_type == article 时）
    slug: str | None = Field(default=None, max_length=200, pattern=r"^[a-z0-9-]+$")
    publisher: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    keywords: list[str] = Field(default_factory=list)
    # 专栏连载（content_type == column_post）
    column_id: uuid.UUID | None = Field(default=None)
    # QA 提问（content_type == qa）
    qa_question_id: uuid.UUID | None = Field(default=None)
    status: str = Field(default="published", pattern="^(draft|pending|published)$")
    is_pinned: bool = False
    is_featured: bool = False


class ContentCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    parent_id: uuid.UUID | None = Field(default=None)


class ContentReportCreate(BaseModel):
    """提交举报的入参。

    ``target_title`` 刻意**不在入参里**：由后端从内容行回填，见 service.report_content。
    """

    target_type: str = Field(..., pattern="^(post|comment)$")
    target_id: uuid.UUID
    reason: str = Field(..., min_length=1, max_length=200)


class ContentCommentInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    author_id: uuid.UUID = Field(validation_alias="user_id")
    author_name: str = ""
    content: str
    floor_number: int
    parent_id: uuid.UUID | None = None
    like_count: int
    # 当前读口所属用户是否点过赞；未登录/未指定 viewer 时恒 False
    liked: bool = False
    created_at: datetime.datetime
