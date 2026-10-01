from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import (  # 注意 db.base 而非 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class UserBalance(Base):
    __tablename__: str = "user_balances"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True
    )  # S5: auth user_id
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class PointsLedger(UUIDPrimaryKeyMixin, Base):
    """积分流水账本。

    **复合主键 ``(created_at, id)``**：本表是 TimescaleDB hypertable（按 ``created_at``
    时间分区，见 ``app/db/init_db.py``），而 hypertable 的**每个唯一索引都必须包含分区列**
    ——故 ``id`` 不再是单列主键，幂等键的唯一约束也由 ``(user_id, ref_type, ref_id)`` 放宽为
    ``(user_id, ref_type, ref_id, created_at)``（约束名不变，``pg_upsert`` 按名解析 arbiter）。
    ``id`` 仍是 uuid7（全局唯一），语义未变。

    **幂等弱化的已知面（与 ``outbox_events`` 同款取舍）**：并入 ``created_at`` 后，同一
    ``(user_id, ref_type, ref_id)`` 的两次投递因 ``created_at`` 不同而**不再撞唯一约束**，
    DB 级兜底由「强保证」降为「同微秒才生效」。实际幂等改由 ``reward()`` 的**按用户行锁 +
    ``get_by_ref`` 预检**承担（见 ``points/service.py``）——行锁已把同一 ref 的并发投递串行化，
    唯一约束本就只是锁失效时的兜底。无 timescaledb 的普通表上本约束同样成立（多一列无副作用）。
    """

    __tablename__: str = "points_ledger"
    # 幂等键 (user_id, ref_type, ref_id) + 分区列 created_at（hypertable 要求唯一索引含分区列）
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint(
            "user_id", "ref_type", "ref_id", "created_at", name="uq_points_ledger_ref"
        ),
        # 排行榜日/周窗口聚合：`created_at >= since AND delta > 0`，命中索引免全表扫
        Index("ix_ledger_created_delta", "created_at", "delta"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, primary_key=True
    )


class UserBehaviorStat(Base):
    __tablename__: str = "user_behavior_stats"
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True
    )  # S5: auth user_id
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
    __table_args__ = (
        # -1 是「无限/虚拟」哨兵，而消费侧只判 `stock < 0`：-5 这类值会被同样当成无限，
        # 悄悄把限量商品变成不限量。DB 层挡住 < -1 的写入（0 与正数=限量，-1=无限）。
        CheckConstraint("stock >= -1", name="ck_exchange_stock"),
    )
    key: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # e1..e6
    name_key: Mapped[str] = mapped_column(String(120), nullable=False)
    desc_key: Mapped[str] = mapped_column(String(200), nullable=False)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stock: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1
    )  # -1 无限/虚拟
    is_virtual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
