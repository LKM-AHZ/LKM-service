"""search REST 出参：命中项内联内容摘要与计数，避免前端逐条回拉详情（N+1）。"""

import datetime
import uuid

from pydantic import BaseModel


class SearchHit(BaseModel):
    """统一内容检索命中项（字段与内容列表项对齐，便于前端复用卡片渲染）。"""

    id: uuid.UUID
    content_type: str
    board_id: uuid.UUID
    title: str
    excerpt: str = ""
    slug: str | None = None
    author_id: uuid.UUID | None = None
    author_name: str = ""
    # 三个计数由 search/service.py 从 content_items 行显式映射（上游列 non-null + default 0），
    # 声明成必填：将来映射漏字段会立刻报错，而不是静默序列化出一个看似合理的 0
    like_count: int
    comment_count: int
    view_count: int
    published_at: datetime.datetime | None = None
    created_at: datetime.datetime
