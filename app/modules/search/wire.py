"""search 读响应的 msgspec Struct 镜像（B6c，roadmap §6.5.2 扩面）。

search 是**命中列表**端点（多字段 × N 条），与 timeline 同属 msgspec 收益面：Pydantic
``PageData[SearchHit]`` 仍负责构造/校验，本模块只做「校验后 → 可 msgspec 直编」的镜像。

字段名与 Pydantic 逐一对齐且保持 **snake_case**（与 ``feed/wire.py`` 同理：不按蓝图 §6.5.2
字面用 ``to_camel``，既有前端契约消费 snake_case）。

编码等价性（datetime/UUID/None）由 ``tests/test_read_msgspec.py`` 的参数化用例守。
"""

from __future__ import annotations

import datetime
import uuid

import msgspec

from app.core.common import PageData
from app.modules.search.schemas import SearchHit


class SearchHitWire(msgspec.Struct):
    """``SearchHit`` 的 msgspec 镜像（字段同序同名）。"""

    id: uuid.UUID
    content_type: str
    board_id: uuid.UUID
    title: str
    excerpt: str
    slug: str | None
    author_id: uuid.UUID | None
    author_name: str
    like_count: int
    comment_count: int
    view_count: int
    published_at: datetime.datetime | None
    created_at: datetime.datetime


class SearchPageWire(msgspec.Struct):
    """``PageData[SearchHit]`` 的 msgspec 镜像。"""

    items: list[SearchHitWire]
    total: int
    page: int
    pages: int


def to_wire(page: PageData[SearchHit]) -> SearchPageWire:
    """Pydantic 分页（已校验）→ msgspec 镜像；显式取字段，避免 ``model_dump`` 开销。"""
    return SearchPageWire(
        items=[
            SearchHitWire(
                id=hit.id,
                content_type=hit.content_type,
                board_id=hit.board_id,
                title=hit.title,
                excerpt=hit.excerpt,
                slug=hit.slug,
                author_id=hit.author_id,
                author_name=hit.author_name,
                like_count=hit.like_count,
                comment_count=hit.comment_count,
                view_count=hit.view_count,
                published_at=hit.published_at,
                created_at=hit.created_at,
            )
            for hit in page.items
        ],
        total=page.total,
        page=page.page,
        pages=page.pages,
    )
