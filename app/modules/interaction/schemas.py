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


class _ContentSummaryItem(BaseModel):
    """收藏/历史共用的内容摘要字段：形状只在一处声明，避免两边各自漂移。

    字段顺序保持与原两个模型一致（基类字段在前），JSON 出参顺序不变。
    """

    content_id: uuid.UUID
    content_type: str
    title: str
    slug: str | None = None
    board_id: uuid.UUID


class FavoriteItem(_ContentSummaryItem):
    created_at: datetime.datetime


class ViewState(BaseModel):
    """浏览上报结果（upsert 幂等：重复上报只刷新 viewed_at）。"""

    content_id: uuid.UUID
    viewed_at: datetime.datetime


class HistoryItem(_ContentSummaryItem):
    viewed_at: datetime.datetime


# ---- 关注关系（原 follow 域，随归属迁入 interaction）----


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
