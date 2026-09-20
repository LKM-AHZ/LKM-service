import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from auth.schemas import Password


class AdminLoginReq(BaseModel):
    """后台登录请求体（与前台同构，密码走同一套 Password 校验/哈希）。"""

    username: str = Field(..., min_length=1, max_length=100)
    password: Password = Field(...)


class AdminVerify2FARequest(BaseModel):
    """后台危险操作 step-up：提交的 6 位 TOTP 码。"""

    code: str = Field(..., min_length=6, max_length=6)


class AdminUserOut(BaseModel):
    """后台返回的管理员自身信息。仅暴露登录所需的少量字段，不含敏感 PII。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    account_level: str
    created_at: datetime.datetime


class AdminUserListItem(BaseModel):
    """后台用户列表项。默认不含邮箱/手机等 PII；include_pii=True 时才带。"""

    id: uuid.UUID
    username: str
    account_level: str
    is_locked: bool
    created_at: datetime.datetime
    # PII（默认隐藏）
    email: str | None = None
    phone: str | None = None


class AdminStats(BaseModel):
    """后台仪表盘聚合统计。"""

    user_count: int
    post_count: int
    file_count: int
    file_pending_count: int


class AdminReportListItem(BaseModel):
    """后台举报列表项。type: post/comment/file；status: pending/resolved/dismissed。"""

    id: uuid.UUID
    type: str
    target_id: str
    target_title: str
    reporter_name: str
    reason: str
    status: str
    created_at: datetime.datetime
    handled_at: datetime.datetime | None = None


class AdminTrendItem(BaseModel):
    """后台趋势点：某自然日新增的注册用户数与帖子数。"""

    date: datetime.date
    user_delta: int
    post_delta: int


class AdminBotSsoTicket(BaseModel):
    """bot 面板单点登录票据：后台 SSR 页拿它拼 iframe URL，由 bot 面板一次性消费。

    票据是短期（60s）且一次性的**换取凭据**，只用于换 bot 面板自身会话，不是社区侧授权凭据；
    故无 include_pii 之类门控，但也绝不落库、不进日志。
    """

    ticket: str
    expires_in: int


class DimUserRow(BaseModel):
    """离线报表宽表（``user_dim``，M3.B0.3）的只读 accounting 行。

    供运营/报表/accounting 这类批式、可容忍 sync_ts 滞后、绝不容忍 PII 横向散布的读方
    （B0.3 read port，见 ``app.modules.admin.dim_report``）。字段为 user_dim 反范式副本的
    非 PII accounting 列 + (gate 开启时才带) 镜像 email。**本行绝不驱动任何管理/改动作**：
    运营/报表读它绝无行级写会话；在线管理/动作列表继续走 A4 auth 实时缝。

    email 语义镜像 user_dim.email（离线副本，非可写源）；按与 A4 ``AdminUserListItem``
    相同的 ``include_pii`` 布尔门控——默认不投影/不带，避免离线副本 PII 经报表横向散布。
    sync_ts 供读方判该行离最后一次 ETL/事件刷新多近（可容忍滞后）。
    """

    user_id: uuid.UUID
    username: str
    account_level: str
    is_banned: bool
    is_locked: bool
    created_at: datetime.datetime
    sync_ts: datetime.datetime
    nickname: str | None = None
    role: str | None = None
    # PII（离线副本镜像；include_pii=True 才投影）
    email: str | None = None
