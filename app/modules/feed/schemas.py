"""信息流(feed)域请求/响应模型：关注关系(follow) + 时间线 read 聚合产物。

关注关系模型 (FollowToggle/FollowState/FollowUser/FollowBoard) 原属 follow 域；
时间线统一条目/分页响应 (FeedItem/FeedResponse) 原属 timeline 域。随 M2.3 合入单一
feed 域，两类无重名、语义独立，合居此文件。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class FollowToggle(BaseModel):
    """follow/unfollow 操作结果：follower 当前是否正关注该目标。"""

    following: bool


class FollowState(BaseModel):
    """查询某目标对当前用户的关注状态（follow/unfollow 之外的可选展示）。"""

    is_following: bool


class FollowUser(BaseModel):
    """「我关注的用户」列表项。"""

    user_id: uuid.UUID
    display_name: str
    avatar: str | None = None


class FollowBoard(BaseModel):
    """「我关注的版块」列表项。"""

    board_id: uuid.UUID
    title: str


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
