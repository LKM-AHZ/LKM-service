import datetime
import uuid
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth.schemas import ProfileInfo


class ArticleListItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    slug: str
    title: str
    description: str | None = None
    cover: str | None = None
    category_id: uuid.UUID
    category_title: str = ""
    published: datetime.datetime | None = None
    views: int = 0
    likes: int = 0
    comments: int = 0


class ArticleDetail(ArticleListItem):
    bookmarks: int = 0
    department: str | None = None
    publisher: str | None = None
    content: str
    reading_time: int = 0
    keywords: list[str] = []
    tags: list[str] = []

    @field_validator("keywords", mode="before")
    @classmethod
    def _split_keywords(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        if isinstance(v, list):
            return cast(list[str], v)
        return []


class CategoryCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    title: str = Field(..., min_length=1, max_length=100)
    sort: int = Field(default=0, ge=0)


class CategoryOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    sort: int


class ArticleCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=200, pattern=r"^[a-z0-9-]+$")
    description: str | None = Field(default=None, max_length=2000)
    cover: str | None = Field(default=None, max_length=2000)
    content: str = Field(..., min_length=1)
    category_id: uuid.UUID = Field(...)
    keywords: list[str] = Field(default_factory=list)
    department: str | None = Field(default=None, max_length=100)
    publisher: str | None = Field(default=None, max_length=100)
    status: str = Field(default="draft", pattern="^(draft|pending|published)$")
    tags: list[str] = Field(default_factory=list)


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    cover: str | None = Field(default=None, max_length=2000)
    content: str | None = Field(default=None, min_length=1)
    category_id: uuid.UUID | None = Field(default=None)
    keyword_str: str | None = Field(default=None, max_length=2000)  # 逗号分隔
    status: str | None = Field(
        default=None, pattern="^(draft|pending|published|rejected)$"
    )
    tags: list[str] | None = None


class ReviewArticleRequest(BaseModel):
    approve: bool


class ArticleCategory(BaseModel):
    slug: str
    name: str
    article_count: int


class ArticleLikeStatus(BaseModel):
    liked: bool
    like_count: int


class ArticleCommentCreate(BaseModel):
    content: str
    parent_id: uuid.UUID | None = None


class ArticleCommentOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    article_id: uuid.UUID
    user_id: uuid.UUID
    content: str
    parent_id: uuid.UUID | None = None
    created_at: datetime.datetime
    profile: ProfileInfo | None = None
