"""信息流(feed)域响应模型：时间线 read 聚合产物。

关注关系出参 (FollowToggle/FollowState/FollowUser/FollowBoard) 已随归属迁入
``app.modules.interaction.schemas``（蓝图 §7.2 目标形态），本文件只剩时间线条目/分页。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class FeedItem(BaseModel):
    """统一 feed 条目（跨源归一后）。"""

    item_type: str  # discussion | article | column | qa | project | blog
    id: uuid.UUID
    # Pydantic v2 里 `X | None` 不带默认值仍是**必填**（None 合法 ≠ 可缺省），与同文件
    # board_id 的写法也不一致；补 = None 让注解与「Article 无作者 → None」的口径一致。
    author_id: uuid.UUID | None = None  # Article 无作者外键 → None
    author_name: str
    title: str
    content_preview: str
    created_at: datetime
    sort_score: float
    board_id: uuid.UUID | None = None
    url: str


class FeedResponse(BaseModel):
    """时间线响应：条目 + 下一页游标。

    ``next_cursor`` 为 None 表示已到末尾；游标为 Base64 编码的
    ``"{iso_time}|{id}"``，按 (created_at, id) 下滤。
    """

    items: list[FeedItem]
    next_cursor: str | None = None  # None == 已到末尾（同上：补默认值才真的可缺省）
