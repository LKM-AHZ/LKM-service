"""outbox 已发布冷表（M6.3，路线图 §9.3）。

`outbox_events` 只承载「待投递」窗口：投递成功的行若长期滞留，表会随时间无限增长
（领取窗口索引随之膨胀）。relay 按保留期把**已 published 且超期**的行先复制到本表、
再从 `outbox_events` 删除——「先归档后删」保证删除可追溯（蓝图为该不可逆操作定的纪律），
本表即那份冷副本，供排查「某事件何时投出」与将来人工重放。

与两张既有表的边界：
- `event_failures`：relay **投不出**（重试耗竭/永久失败）的归档，是故障域；
- `outbox_archived`（本表）：relay **投成功**后的过期归档，是正常生命周期终点。

保留期与批大小由 relay 侧配置（``LKM_OUTBOX_ARCHIVE_RETENTION_S`` / ``_BATCH``）决定，
本模型不设默认策略。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso


class OutboxArchived(UUIDPrimaryKeyMixin, Base):
    """已投递 outbox 事件的冷副本；`event_id` 为跨表审计锚点（与 outbox_events 同值）。

    **复合主键 ``(created_at, id)``**：本表同 `outbox_events` 一样是 TimescaleDB
    hypertable（按 ``created_at`` 分区 + 列式压缩），hypertable 的唯一索引必须含分区列，
    故主的 `id` 不再是单列主键。本表**不设保留策略**——它的职责就是「可查历史」，
    由 `outbox_events` 的保留策略与归档链路约束其增长（见 §8 #40）。
    """

    __tablename__: str = "outbox_archived"

    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # primary_key=True：与 mixin 的 id 组成复合主键 (created_at, id)，见类 docstring。
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, primary_key=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, default=None
    )
    # 迁入本表的时刻（与 published_at 区分：前者是投出时刻，后者是归档动作时刻）
    archived_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    __table_args__: tuple = (Index("ix_outbox_archived_published_at", "published_at"),)
