"""interaction REST 出参。列表项内联内容摘要（title/slug/content_type），
避免前端为每条收藏再逐个拉详情（N+1）。"""

import datetime
import uuid
from typing import Literal

from pydantic import BaseModel, Field


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


# ---- Block / Mute ----


class BlockToggle(BaseModel):
    """block/unblock 操作结果。"""

    blocked: bool


class MuteToggle(BaseModel):
    """mute/unmute 操作结果。"""

    muted: bool


class RelationshipState(BaseModel):
    """当前用户与目标用户的关系：我屏蔽对方 / 对方屏蔽我 / 我静音对方。"""

    blocked: bool
    blocked_by: bool
    muted: bool


# ---- 统一内容订阅 + 单帖 watch ----


class SubscriptionTarget(BaseModel):
    """订阅/取消订阅目标。

    ``target_type`` 取 ``board|tag|column``；tag 由服务端 trim + 小写归一。
    """

    target_type: str
    target_id: str = Field(min_length=1, max_length=255)
    notify_level: Literal["all", "none"] = "all"


class SubscriptionToggle(BaseModel):
    """订阅/取消订阅操作结果：``subscribed`` 表示当前是否已订阅。"""

    subscribed: bool


class SubscriptionItem(BaseModel):
    """「我的订阅」列表项。"""

    target_type: str
    target_id: str
    notify_level: str
    created_at: datetime.datetime


class WatchToggle(BaseModel):
    """watch/unwatch 操作结果：``watching`` 表示当前是否在 watch。"""

    watching: bool


class WatchItem(_ContentSummaryItem):
    """「我的 watch」列表项。"""

    notify_level: str
    created_at: datetime.datetime


# ---- 好友关系（desktop 的 Stargate 兼容出参）----
#
# desktop 端直接以 ``SnRelationship.fromJson`` / ``SnFriendOverviewItem.fromJson``
# 解析这些响应（非 ApiResp 信封），字段名与嵌套形状必须逐字兼容，故按 Stargate
# 的 snake_case 契约单独建模；业务出参模型不参与上面统一信封。

FRIEND_STATUS_PENDING = 0
FRIEND_STATUS_FRIEND = 100
FRIEND_STATUS_CLOSE_FRIEND = 200
FRIEND_STATUS_MUTED = -50
FRIEND_STATUS_BLOCKED = -100


class RelationshipStatusUpdate(BaseModel):
    """``PATCH /stargate/relationships/{id}`` 的请求体。"""

    status: int


class RelationshipAliasUpdate(BaseModel):
    """``PATCH /stargate/relationships/{id}/alias`` 的请求体。"""

    alias: str | None = Field(default=None, max_length=128)


class RelationshipBlockRequest(BaseModel):
    """``POST /stargate/relationships/{id}/block`` 的请求体。"""

    expires_in: str | None = Field(default=None, max_length=16)
    degrade_to: int | None = None


class RelationshipMuteRequest(BaseModel):
    """``POST /stargate/relationships/{id}/mute`` 的请求体。"""

    expires_in: str | None = Field(default=None, max_length=16)


class StargateAccountProfileOut(BaseModel):
    """``SnAccountProfile`` 的最小子集：必填字段齐全，其余取默认。"""

    id: str
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    bio: str = ""
    gender: str = ""
    pronouns: str = ""
    location: str = ""
    time_zone: str = ""
    birthday: datetime.datetime | None = None
    links: list[dict] = Field(default_factory=list)
    last_seen_at: datetime.datetime | None = None
    active_badge: dict | None = None
    experience: int = 0
    level: int = 0
    social_credits: float = 100
    social_credits_level: int = 0
    leveling_progress: float = 0
    picture: dict | None = None
    background: dict | None = None
    verification: dict | None = None
    username_color: dict | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    deleted_at: datetime.datetime | None = None


class StargateAccountOut(BaseModel):
    """``SnAccount`` 的最小子集：desktop 解析要求必填字段均在。"""

    id: str
    name: str
    nick: str
    language: str = "zh_CN"
    region: str = ""
    is_superuser: bool = False
    automated_id: str | None = None
    profile: StargateAccountProfileOut
    perk_subscription: dict | None = None
    badges: list[dict] = Field(default_factory=list)
    contacts: list[dict] = Field(default_factory=list)
    activated_at: datetime.datetime | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    deleted_at: datetime.datetime | None = None


class StargateRelationshipOut(BaseModel):
    """``SnRelationship`` 兼容出参。

    列表场景中 ``account`` / ``related`` 都填「对方」，``account_id`` /
    ``related_id`` 也统一指对方——desktop 的 PATCH/DELETE 等动作都用这两个字段定位目标。
    请求列表则保留真实方向，供前端区分收到的请求与已发出的请求。
    """

    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    deleted_at: datetime.datetime | None = None
    account_id: str
    account: StargateAccountOut | None = None
    related_id: str
    related: StargateAccountOut | None = None
    expired_at: datetime.datetime | None = None
    status: int
    alias: str | None = None
    degrade_to_status: int | None = None


class StargateAccountStatusOut(BaseModel):
    """``SnAccountStatus`` 兼容出参；``is_online`` 来自 ``/ws/events`` 长连接。"""

    id: str
    attitude: int = 0
    is_online: bool = False
    is_idle: bool = False
    idle_since: datetime.datetime | None = None
    is_customized: bool = False
    type: int = 0
    label: str = ""
    symbol: str | None = None
    icon: dict | None = None
    background: dict | None = None
    meta: dict | None = None
    cleared_at: datetime.datetime | None = None
    app_identifier: str | None = None
    is_automated: bool = False
    account_id: str
    account: StargateAccountOut | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    deleted_at: datetime.datetime | None = None
    online_devices: list[dict] = Field(default_factory=list)


class StargateFriendOverviewOut(BaseModel):
    """``SnFriendOverviewItem`` 兼容出参。"""

    account: StargateAccountOut
    status: StargateAccountStatusOut
    activities: list[dict] = Field(default_factory=list)
