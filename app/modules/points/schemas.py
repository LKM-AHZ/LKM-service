import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class BalanceOut(BaseModel):
    user_id: uuid.UUID
    balance: int


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
