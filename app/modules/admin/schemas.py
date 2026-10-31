import datetime
import uuid
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.contracts import Password


class AdminLoginReq(BaseModel):
    """后台登录请求体（与前台同构，密码走同一套 Password 校验/哈希）。"""

    username: str = Field(..., min_length=1, max_length=100)
    password: Password = Field(...)


class AdminVerify2FARequest(BaseModel):
    """后台危险操作 step-up：提交的 6 位 TOTP 码。

    仅限长度的校验会放过 "abcdef" 这类 6 位非数字串，错误只能在下游校验里以「验证码不对」
    的含糊形态暴露；用 pattern 把「必须 6 位数字」的契约在边界上写死。
    """

    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


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


class AdminReviewQueueItem(BaseModel):
    """单个待办队列：数量 + 最旧一条的等待时长（秒）+ 审核页深链。"""

    key: str
    count: int
    oldest_waiting_seconds: int | None = None
    # 暂无后台审核页的队列为 None（避免前端链到 404）
    href: str | None = None


class AdminReviewSummary(BaseModel):
    """后台待办审核聚合（dashboard 卡片与每日摘要共用）。"""

    total_pending: int
    queues: list[AdminReviewQueueItem]


class AdmissionUserItem(BaseModel):
    """准入审核行（auth 权威侧投影；时间已是 ISO 字符串）。"""

    user_id: str
    username: str
    email: str | None = None
    account_level: str
    admission_status: str
    apply_reason: str | None = None
    applied_at: str | None = None
    reviewed_at: str | None = None
    reject_reason: str | None = None


class AdmissionDecisionIn(BaseModel):
    action: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=500)


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


class PointsReportPoint(BaseModel):
    """积分度量/行为报表的一个日桶 × 行为类型数据点。

    来源是 continuous aggregate ``points_daily``（``time_bucket('1 day')`` × ``reason``），
    故 ``day`` 是 UTC 日桶起点。``reason`` 即行为类型（``points/rules.py::RULE_DELTAS``
    的键：post/comment/like/file_approved/...）。
    """

    day: datetime.date
    reason: str
    delta_sum: int
    entry_count: int


class PointsReportOut(BaseModel):
    """积分度量/行为报表（离线面）。

    **读的是连续聚合物化视图，不是实时真值**：数据新鲜度由 TimescaleDB 的刷新策略决定
    （装配按 1 小时刷新、``end_offset`` 1 小时），实时积分/排行榜请走 ``/points/leaderboard``。
    """

    days: int
    series: list[PointsReportPoint]
    total_delta: int
    total_entries: int


class AdminBotOverview(BaseModel):
    """后台「机器人」原生页面概览（服务侧 BFF 聚合 LKM-bot 面板数据）。

    ``configured`` = 服务端是否配置了 bot 地址与服务密钥；``available`` = 本次取数是否成功。
    两者都为 False 时页面降级为「面板未接入」，不抛 500——监控数据不可用不该拖垮后台。
    """

    configured: bool
    available: bool
    reason: str | None = None
    version: str | None = None
    dashboard_version: str | None = None
    platform_count: int | None = None
    provider_count: int | None = None
    plugin_count: int | None = None
    message_count: int | None = None
    cpu_percent: float | None = None
    memory_process_mb: int | None = None
    memory_system_mb: int | None = None
    thread_count: int | None = None
    running: dict[str, Any] | None = None


class AdminBotLogHistory(BaseModel):
    """后台日志页的历史回填（BFF 代理 bot 的 `/logs/history`）。

    与概览一致：未配置/不可达时用 ``configured`` / ``available`` 表达，不抛 5xx。
    """

    configured: bool
    available: bool
    reason: str | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list)


class AdminBotPipInstallRequest(BaseModel):
    """后台日志页的 pip 安装请求（与 bot `PipInstallRequest` 同形）。"""

    package: str | None = None
    mirror: str | None = None


class AdminBotPlatformItem(BaseModel):
    """机器人平台实例（面板 `/bots` 的最小投影）。"""

    id: str
    type: str | None = None
    display_name: str | None = None
    enabled: bool = False


class AdminBotPlatforms(BaseModel):
    """后台「机器人平台」页数据（配置/可用标记 + 平台列表）。"""

    configured: bool
    available: bool
    reason: str | None = None
    items: list[AdminBotPlatformItem] = Field(default_factory=list)


class AdminBotProviderItem(BaseModel):
    """模型提供商（面板 `/providers` 的最小投影）。"""

    id: str
    type: str | None = None
    model: str | None = None
    enabled: bool = False


class AdminBotProviders(BaseModel):
    """后台「模型提供商」页数据。"""

    configured: bool
    available: bool
    reason: str | None = None
    items: list[AdminBotProviderItem] = Field(default_factory=list)


class AdminBotEnabledPatch(BaseModel):
    """启用/停用补丁（与面板 `EnabledPatch` 同形）。"""

    enabled: bool


class AdminBotListItem(BaseModel):
    """通用列表项：插件/知识库等页面共用的最小投影。"""

    id: str
    name: str
    subtitle: str | None = None
    enabled: bool | None = None


class AdminBotList(BaseModel):
    """通用资源列表（configured/available 降级 + items）。"""

    configured: bool
    available: bool
    reason: str | None = None
    items: list[AdminBotListItem] = Field(default_factory=list)
