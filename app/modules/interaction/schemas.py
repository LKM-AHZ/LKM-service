"""interaction REST 出参。列表项内联内容摘要（title/slug/content_type），
避免前端为每条收藏再逐个拉详情（N+1）。"""

import datetime
import uuid

from pydantic import BaseModel


class FavoriteState(BaseModel):
    """收藏/取消后的即时状态，供前端就地更新按钮与计数。"""

    content_id: uuid.UUID
    favorited: bool
    bookmark_count: int


class FavoriteItem(BaseModel):
    content_id: uuid.UUID
    content_type: str
    title: str
    slug: str | None = None
    board_id: uuid.UUID
    created_at: datetime.datetime


class ViewState(BaseModel):
    """浏览上报结果（upsert 幂等：重复上报只刷新 viewed_at）。"""

    content_id: uuid.UUID
    viewed_at: datetime.datetime


class HistoryItem(BaseModel):
    content_id: uuid.UUID
    content_type: str
    title: str
    slug: str | None = None
    board_id: uuid.UUID
    viewed_at: datetime.datetime
