"""内容项的公开输出契约（跨域共享 DTO）。

`admin` 的内容列表接口直接把它当作 ``response_model``，`content` 域自己也用它做出参。
放在 ``app.contracts`` 是因为**两个业务域都要引用同一份形态**：留在 content 会让 admin
产生跨模块 import，复制一份则两边形态必然漂移。
"""

from __future__ import annotations

import datetime
import uuid
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, field_validator

from core.common import parse_tags


class ContentItemInfo(BaseModel):
    """统一内容项输出。``author_name`` 优先 user FK 的昵称，否则回退 publisher。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_type: str
    board_id: uuid.UUID
    author_id: uuid.UUID | None = None
    author_name: str = ""
    publisher: str | None = None
    department: str | None = None
    column_id: uuid.UUID | None = None
    column_title: str = ""
    qa_question_id: uuid.UUID | None = None
    slug: str | None = None
    title: str
    excerpt: str
    summary: str | None = None
    cover: str | None = None
    keywords: list[str] = []
    content: str
    tags: list[str] = []
    status: str = "published"
    is_pinned: bool = False
    is_featured: bool = False
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    bookmark_count: int = 0
    forward_count: int = 0
    reading_time: int = 0
    created_at: datetime.datetime
    published_at: datetime.datetime | None = None
    # 乐观锁版本号（蓝图 §6.1）：客户端可见，编辑时回带。
    version: int = 1

    @field_validator("tags", "keywords", mode="before")
    @classmethod
    def _parse_list(cls, v: object) -> list[str]:
        if isinstance(v, list):
            return cast(list[str], v)
        if isinstance(v, str):
            if v.startswith("["):
                return parse_tags(v)
            return [k.strip() for k in v.split(",") if k.strip()]
        return []
