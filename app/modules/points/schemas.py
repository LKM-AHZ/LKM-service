import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class BalanceOut(BaseModel):
    user_id: uuid.UUID
    balance: int
    # 等级由积分余额折算：每 100 分一级，满级进度按当前级内区间计算。
    level: int = 1
    next_level_points: int = 100
    level_progress: int = 0


class LedgerEntry(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    delta: int
    balance_after: int
    reason: str
    ref_type: str
    ref_id: str
    created_at: datetime.datetime


class LeaderboardEntry(BaseModel):
    user_id: uuid.UUID
    display_name: str
    balance: int
    title: str = (
        "active"  # 稳定 key，前端 i18n 映射 contributionData.leaderboard.titles.*
    )


class AchievementOut(BaseModel):
    id: uuid.UUID
    key: str
    category: str
    icon: str
    name_key: str
    desc_key: str
    type: str
    threshold: int
    reward_points: int
    sort_order: int
    progress: int = 0
    unlocked: bool = False


class TaskOut(BaseModel):
    id: uuid.UUID
    key: str
    title_key: str
    desc_key: str
    category: str
    requirement_count: int
    reward_points: int
    sort_order: int
    current_progress: int = 0
    completed: bool = False


class ExchangeItemOut(BaseModel):
    id: uuid.UUID
    key: str
    name_key: str
    desc_key: str
    points_cost: int
    stock: int
    is_virtual: bool
    sort_order: int


class CheckInTipOut(BaseModel):
    title: str
    content: str
    is_positive: bool


class CheckInFortuneOut(BaseModel):
    version: int = 1
    poem: str
    summary: str
    summary_detail: str | None = None
    wish: str
    love: str
    study: str
    career: str
    health: str
    lost_item: str
    lucky_color: str
    lucky_direction: str
    lucky_time: str
    lucky_item: str
    lucky_action: str
    avoid_action: str
    ritual: str


class CheckInResultOut(BaseModel):
    id: uuid.UUID
    level: int
    tips: list[CheckInTipOut]
    fortune_report: CheckInFortuneOut
    created_at: datetime.datetime


class CheckInHistoryItem(BaseModel):
    date: datetime.date
    result: CheckInResultOut
