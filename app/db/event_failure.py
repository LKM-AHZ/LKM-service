"""outbox 发布失败归档表（M1 gate review 收口，路线图 §4 M1.3）。

relay 有**两条**折叠路径把行从 `outbox_events` **摘除**迁入本表——都是「删原行」而**不是**
置某个终态（`outbox_events` 不会出现 `status=failed` 的行，本表也没有 status 列）：

1. **瞬时失败耗竭**：投递反复失败致 ``attempt_count`` 达 `MAX_TRIES` 后不再重投，
   ``reason="relay exhausted: max tries reached"``，此类 ``attempt_count >= MAX_TRIES``。
2. **确定性永久失败**：投递前经 ``messaging.permanent_failure_reason`` 判定为「重试无意义」
   （未知 routing_key / payload 不可 JSON 编码）→ **首次尝试即折叠**，不消耗重试额度，
   故此类行的 ``attempt_count`` 可能为 0。

**查询/过滤不得假定 ``attempt_count >= MAX_TRIES``**（第 2 类不满足此不变量）；要区分两类看
``reason``。折叠后不再挤占 relay 领取窗口/积压 gauge，留一份审计副本供排查与未来人工重放。
与消费侧 DMQ(`dlq_messages`) 故障域隔离：本表只管「relay 发布侧投不出」，消费侧失败仍进
Pulsar 死信 topic（system/dlq）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso
from app.db.outbox import enqueue_outbox


class EventFailure(UUIDPrimaryKeyMixin, Base):
    """relay 发布失败（重试耗竭 **或** 确定性永久失败）而迁出的归档事件。

    ``event_id`` 即审计锚点；两条折叠路径及其 attempt_count 取值差异见模块 docstring。
    """

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


async def replay_failure(db: AsyncSession, failure_id: uuid.UUID) -> bool:
    """把一条归档失败**重新入队 outbox** 并摘除归档行（蓝图 §5.1 第 3 条的人工重放通道）。

    该条要求「`event_failures` 表 + 告警驱动人工/脚本修复后重放」——此前只有只读分析入口
    （ClickHouse 数据集），运维改完根因后没有可执行的重放手段。

    语义与取舍：
    - **沿用原 ``event_id``**：它是事件链路的幂等键，下游按它去重——若此前已有部分投递，
      重放不会造成重复结算；relay 的 best-effort 去重也会把「同 id 已在途」视为重复跳过。
    - **改走 outbox 而非直投总线**：重放要的是「可靠投递」语义（DB 成、事件必达），直投
      绕开发件箱会重新引入丢事件窗口。
    - **总线未启用时不删归档行**并返回 False：投不出去的情况下把唯一的审计副本删掉就是
      数据丢失。调用方据此回报「不可用」而非「成功」。
    - 行级 ``FOR UPDATE``：并发两次重放只有一条真正入队（后到者拿到已删行 → False）。
    """
    row = await db.scalar(
        select(EventFailure).where(EventFailure.id == failure_id).with_for_update()
    )
    if row is None:
        return False
    if not settings.message_bus_enabled:
        return False
    # event_id 复用（见上）；同 id 已在途时 enqueue_outbox 返回 False——此时归档副本已无
    # 意义，照常摘除即可（事件确实在投递队列里）。
    await enqueue_outbox(
        db, row.routing_key, dict(row.payload_json), event_id=row.event_id
    )
    await db.delete(row)
    await db.flush()
    return True
