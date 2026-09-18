"""outbox 耗竭失败归档表（M1 gate review 收口，路线图 §4 M1.3）。

relay 对某事件投递反复失败、`attempt_count` 达 `MAX_TRIES` 上限后不再重投：把该行从
`outbox_events` **摘除**迁此表（`status=failed` 终态不再滞留原表、不再挤占 relay 领取
窗口/积压 gauge），留一份审计副本供排查与未来人工重放。与消费侧 DMQ(`dlq_messages`)
故障域隔离：本表只管「relay 发布侧反复失败致投不出」，消费侧失败仍进 Pulsar 死信 topic（system/dlq）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso


class EventFailure(UUIDPrimaryKeyMixin, Base):
    """relay 发布耗竭(outbox attempt>=MAX)而迁移的归档事件。event_id 即审计锚点。"""

    __tablename__: str = "event_failures"

    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 逻辑主题 = 将投失败时的 routing_key
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 与 outbox_events.payload_json 同构的全量 {fn,args} dict；含透传的 event_id
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    folded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
