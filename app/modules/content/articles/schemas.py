import datetime
import uuid
from typing import ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

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
            # 显式拷贝：cast 在运行期是 no-op，直接返回原列表会让响应对象与 ORM 属性
            # 共享同一个 list，调用方一改就污染实例状态
            return [str(k) for k in v]
        return []


class CategoryCreate(BaseModel):
    # 每段之间必须至少一个字母数字：`^[a-z0-9-]+$` 会放过 "-"、"--"、"-a-"、
    # "a--b" 这类退化串（前导/尾随/连续连字符），生成的 URL 难看且易与路由分隔语义混
    slug: str = Field(
        ..., min_length=1, max_length=50, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
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
    # 同 CategoryCreate.slug：拒绝前导/尾随/连续连字符
    slug: str = Field(
        ..., min_length=1, max_length=200, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
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

    @model_validator(mode="after")
    def _reject_null_for_not_null_columns(self) -> "ArticleUpdate":
        """显式传 null 只允许落在可空列：title/content/category_id 在 ORM 里是 NOT NULL，
        而调用方用 model_dump(exclude_unset=True) + setattr 落库，显式 null 会被当成「已设置」
        写进去（轻则清空权威列，重则 flush 时 IntegrityError → 500）。此处改抛 422。
        （tags 显式 null 表示「不改标签」，由 _sync_article_tags 处理，不在此列。）"""
        for name in ("title", "content", "category_id"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不能为 null")
        return self


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
    # 与文章正文一致的边界：无下限会收下空/纯空白评论，无上限则单条评论可任意长
    content: str = Field(..., min_length=1, max_length=2000)
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
