"""死信落库表：``dlq_messages``。

原先挂在 ``app.modules.admin.models`` 下，但死信的**生产方**是 ``core.worker_dlq``
（core 侧进程），若表定义留在 app，core 就不得不反向 import 业务模块。故随 core/boot
拆分下沉至此——表名与列定义与迁移完全一致，admin 侧（``dlq_router``）改从本模块引用。

时间列遵循既有约定：用 ``UTCDateTime`` 类型 + ``now_iso()`` 默认值。勿用裸 ``DateTime``
/ ``datetime.now(UTC)``。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import (
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class DlqMessage(UUIDPrimaryKeyMixin, Base):
    """死信消息落库：worker_dlq 消费 lkm.dlq 队列持久化，供人工重投/审计。"""

    __tablename__: str = "dlq_messages"

    routing_key: Mapped[str] = mapped_column(String(255), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    exchange: Mapped[str] = mapped_column(String(255), default="lkm.events")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    requeued_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
