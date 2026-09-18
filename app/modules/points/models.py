from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (  # 注意 db.base 而非 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class UserBalance(Base):
    __tablename__: str = "user_balances"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # S5: auth user_id
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class PointsLedger(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "points_ledger"
    # (user_id, ref_type, ref_id) 唯一：幂等——同一事件对同一用户不重复发分
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("user_id", "ref_type", "ref_id", name="uq_points_ledger_ref"),
        # 排行榜日/周窗口聚合：`created_at >= since AND delta > 0`，命中索引免全表扫
        Index("ix_ledger_created_delta", "created_at", "delta"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # 幂等引用键：如 ``<question_uuid>:<answer_uuid>``（73 字符）——uuid 化后两个 36 字符
    # uuid 加分隔已超原 String(64)，故加宽
    ref_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class UserBehaviorStat(Base):
    __tablename__: str = "user_behavior_stats"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # S5: auth user_id
    stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_checkin_date: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )  # YYYY-MM-DD
    checkin_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class Achievement(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "achievements"
    key: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # a1..a12
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="special")
    icon: Mapped[str] = mapped_column(String(80), nullable=False, default="tabler:star")
    name_key: Mapped[str] = mapped_column(String(120), nullable=False)
    desc_key: Mapped[str] = mapped_column(String(160), nullable=False)
    type: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # onboarding/post_count/featured_count/accepted_answers/approved_files/checkin_streak/project_count/column_articles
    threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reward_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserAchievement(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "user_achievements"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    achievement_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("achievements.id"), nullable=False
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unlocked_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    __table_args__ = (
        UniqueConstraint("user_id", "achievement_id", name="uq_user_achievement"),
    )


class Task(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "task_definitions"
    key: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # t1..t5
    title_key: Mapped[str] = mapped_column(String(120), nullable=False)
    desc_key: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # checkin/post/answer/like/file_upload
    requirement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reward_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserTaskProgress(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "user_task_progress"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_definitions.id"), nullable=False
    )
    period_date: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # YYYY-MM-DD，每日任务按天
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rewarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        UniqueConstraint(
            "user_id", "task_id", "period_date", name="uq_user_task_period"
        ),
    )


class ExchangeItem(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exchange_items"
    key: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # e1..e6
    name_key: Mapped[str] = mapped_column(String(120), nullable=False)
    desc_key: Mapped[str] = mapped_column(String(200), nullable=False)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stock: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1
    )  # -1 无限/虚拟
    is_virtual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
