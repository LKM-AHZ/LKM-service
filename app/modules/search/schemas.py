"""search REST 出参：命中项内联内容摘要与计数，避免前端逐条回拉详情（N+1）。"""

import datetime

from pydantic import BaseModel


class SearchHit(BaseModel):
    """统一内容检索命中项（字段与内容列表项对齐，便于前端复用卡片渲染）。"""

    id: int
    content_type: str
    board_id: int
    title: str
    excerpt: str = ""
    slug: str | None = None
    author_id: int | None = None
    author_name: str = ""
    like_count: int = 0
    comment_count: int = 0
    view_count: int = 0
    published_at: datetime.datetime | None = None
    created_at: datetime.datetime
