"""
事务发件箱(outbox)模型与入队辅助（M1.1）。
业务把"想可靠投递到消息总线的异步事件"与自身写入放同一事务（将行加入当前会话 commit），
relay（`app/core/outbox_relay.py`）另行新会话领取并经 `core.messaging.publish` 投 Pulsar 后
改 `published`，达成「DB 成、事件必达」的一致性。仅当 ``settings.pulsar_url`` 非空（生产/有
broker）才入队；未配置(dev/测试)直返 False，维持 fail-open 语义、不留积压。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.config import settings
from core.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso

# outbox 状态机：入队即 pending → relay 投成功置 published；投不出的行由 relay **直接摘除**
# 迁入 event_failures（不留 failed 行——`status=failed` 从未被写入，见 event_failure 模块 docstring）
OUTBOX_PENDING = "pending"
OUTBOX_PUBLISHED = "published"
OUTBOX_FAILED = "failed"

# 单事件最多尝试次数（达上限不再投，防无限重试污染总线）；指数退避秒(cap)
MAX_TRIES = 5
_BACKOFF_CAP_S = 3600


class OutboxEventKey(Base):
    """跨 hypertable 分区的全局事件键；与业务行在同一事务写入。"""

    __tablename__ = "outbox_event_keys"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class OutboxMessage(UUIDPrimaryKeyMixin, Base):
    """待投递事件。payload 与业务同事务落库，relay 按 routing_key 投总线后置 published。

    本表按 created_at 分区，复合主键含分区列。全局 event_id 唯一性由普通表
    ``outbox_event_keys`` 保证，避免跨分区重复入队。
    """

    __tablename__: str = "outbox_events"

    # 幂等键：普通表 outbox_event_keys 担保全局唯一，本表约束仍须包含分区列。
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 逻辑主题 = 现有 topic exchange routing_key（event.apply_point/…），relay 按它 publish
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 携带 {fn,args,…} 完整 dict（worker 按 payload["fn"] 分派）；原生 JSONB 存 dict
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OUTBOX_PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # primary_key=True 是与 mixin 的 id 组成复合主键 (created_at, id)：hypertable 的
    # 分区列必须出现在主键里（见类 docstring）。写入仍由 Python 侧 default 提供值。
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, primary_key=True
    )
    next_retry_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    published_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, default=None
    )
    # 每次单行认领写入时间与唯一 fencing token。
    locked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, default=None
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__: tuple = (
        Index("ix_outbox_scan", "status", "next_retry_at"),
        # 原为列上的 unique=True；hypertable 要求唯一索引含分区列，故并入 created_at。
        UniqueConstraint("event_id", "created_at"),
    )


def _backoff_seconds(attempt: int) -> int:
    """第 attempt 次失败后到下次重试的秒数（指数、封顶 1h）。"""
    return min(2 ** int(attempt), _BACKOFF_CAP_S)


def _jsonable(value: Any) -> Any:
    """递归把 UUID 转成字符串——payload 要经 JSONB 落库、再经 Pulsar JSON 编码投递，
    两者都无法编码 UUID 对象（UUID 为主键后 payload 里的 id 必然是 UUID 实例）。

    **消费侧契约**：handler 经事件链路收到的是**字符串形式**的 uuid，而直接调用路径
    传入的是 UUID 对象。SQLAlchemy 的 ``Uuid`` 列对两者都接受（asyncpg 实测兼容字符串），
    故 handler 无需显式还原；但 handler 内不得对 id 参数调用 UUID 专有属性（如 ``.hex``）。
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


async def _legacy_event_exists(db: AsyncSession, event_id: str) -> bool:
    """迁移前未入键表的显式 ID，按现有索引检查热表与两张归档表。"""
    # 延迟导入避免 event_failure -> outbox 的模块级循环依赖。
    from core.db.event_failure import EventFailure
    from core.db.outbox_archive import OutboxArchived

    for model in (OutboxMessage, OutboxArchived, EventFailure):
        if await db.scalar(select(model.event_id).where(model.event_id == event_id).limit(1)):
            return True
    return False


async def enqueue_outbox(
    db: AsyncSession,
    routing_key: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    replay: bool = False,
) -> bool:
    """把一次将投递事件加入当前事务（不 commit；由业务会话统一提交/回滚）。

    - 未配置消息总线（settings.pulsar_url 空）→ 直接 False：维持 fail-open，dev/测试不产生积压。
    - 显式 event_id 由普通表全局去重；人工重放用 replay=True 持键锁后重新入队。
    - payload 须为 worker 可直接分派的完整 dict（含 "fn"/"args"）。

    返回 True=本次已 join 进事务待提交；False=被 gate 跳过或幂等已存在。

    唯一键插入和业务变更同事务提交，回滚时共同回滚。
    """
    if not settings.message_bus_enabled:
        return False

    eid = event_id or uuid.uuid4().hex
    if replay and event_id is None:
        raise ValueError("replay requires an event_id")
    # hypertable 的唯一约束必须带 created_at，因此另用普通表做全局唯一裁决。
    # INSERT 与 outbox 行共用业务事务；回滚时键也回滚。重放持有同一键的行锁，
    # 串行检查活跃行，允许原失败记录重新投递且不产生并发双入队。
    inserted = await db.scalar(
        insert(OutboxEventKey)
        .values(event_id=eid, created_at=now_iso())
        .on_conflict_do_nothing(index_elements=[OutboxEventKey.event_id])
        .returning(OutboxEventKey.event_id)
    )
    if inserted is None and not replay:
        return False
    if (
        inserted is not None
        and event_id is not None
        and not replay
        and await _legacy_event_exists(db, eid)
    ):
        return False
    if replay:
        await db.scalar(
            select(OutboxEventKey.event_id)
            .where(OutboxEventKey.event_id == eid)
            .with_for_update()
        )
        active = await db.scalar(
            select(OutboxMessage.id)
            .where(
                OutboxMessage.event_id == eid,
                OutboxMessage.status == OUTBOX_PENDING,
            )
            .limit(1)
        )
        if active is not None:
            return False

    row = OutboxMessage(
        event_id=eid,
        routing_key=routing_key,
        payload_json=_jsonable(payload),
    )
    db.add(row)
    return True
