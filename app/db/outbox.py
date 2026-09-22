"""事务发件箱(outbox)模型与入队辅助（M1.1）。

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso

# outbox 状态机：入队即 pending → relay 投成功置 published；投不出的行由 relay **直接摘除**
# 迁入 event_failures（不留 failed 行——`status=failed` 从未被写入，见 event_failure 模块 docstring）
OUTBOX_PENDING = "pending"
OUTBOX_PUBLISHED = "published"
OUTBOX_FAILED = "failed"

# 单事件最多尝试次数（达上限不再投，防无限重试污染总线）；指数退避秒(cap)
MAX_TRIES = 5
_BACKOFF_CAP_S = 3600


class OutboxMessage(UUIDPrimaryKeyMixin, Base):
    """待投递事件。payload 与业务同事务落库，relay 按 routing_key 投总线后置 published。

    **复合主键 ``(created_at, id)``**：本表是 TimescaleDB hypertable（按 ``created_at``
    分区，见 ``init_db``），而 hypertable 的**每个唯一索引都必须包含分区列**——故 ``id``
    不再是单列主键，``event_id`` 的唯一约束也由 ``(event_id)`` 放宽为 ``(event_id,
    created_at)``。两列都唯一性不变，语义未变：``id`` 是 uuid7（全局唯一）、``event_id``
    是每次投递的幂等键，同一 ``event_id`` 不可能有两条同毫秒 ``created_at`` 的行。
    """

    __tablename__: str = "outbox_events"

    # 幂等键：投递去重/防重复副作用以此全局 UUID 为准。唯一性由 (event_id, created_at)
    # 复合约束承担（hypertable 要求唯一索引含分区列），故此处不再单列 unique=True。
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
    # 供 M1.2 leader 摄取时置锁；M1.1 relay 单 owner 不 set，仅立列
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


async def enqueue_outbox(
    db: AsyncSession,
    routing_key: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> bool:
    """把一次将投递事件加入当前事务（不 commit；由业务会话统一提交/回滚）。

    - 未配置消息总线（settings.pulsar_url 空）→ 直接 False：维持 fail-open，dev/测试不产生积压。
    - 提供显式 event_id 幂等：若该 id 已存在且仍 pending/published → 视为重复并跳过（不重复入队）。
      失败(failed)项允许以新的投递在后续业务调用再入队。
    - payload 须为 worker 可直接分派的完整 dict（含 "fn"/"args"）。

    返回 True=本次已 join 进事务待提交；False=被 gate 跳过或幂等已存在。

    **该幂等是 best-effort（先查后插，不是 DB 级保证）**：本表是 TimescaleDB hypertable，
    唯一索引必须包含分区列，故唯一约束只能是 ``(event_id, created_at)``（见模型 docstring）
    ——没有「event_id 单列唯一」可用。两个并发事务以同一 event_id 入队（各自 created_at
    不同）都会插入成功 → 事件被投两次（由下游按 event_id 幂等兜底）；若两者的 created_at
    恰好落在同一微秒，第二个会撞唯一索引并以 IntegrityError 打断调用方业务事务（小概率，
    event_id 通常按业务唯一键派生）。要做成 DB 级强保证必须换承载方式（如独立去重表），
    不在本轮范围。
    """
    if not settings.message_bus_enabled:
        return False

    eid = event_id or uuid.uuid4().hex
    if event_id is not None:
        dup = await db.scalar(
            select(OutboxMessage.id).where(
                OutboxMessage.event_id == event_id,
                OutboxMessage.status.in_([OUTBOX_PENDING, OUTBOX_PUBLISHED]),
            )
        )
        if dup is not None:
            return False

    row = OutboxMessage(
        event_id=eid,
        routing_key=routing_key,
        payload_json=_jsonable(payload),
    )
    db.add(row)
    return True
