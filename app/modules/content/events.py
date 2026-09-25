"""内容域领域事件（``content.*``）：外部检索引擎增量同步的单一数据源。

内容落库与「事件入队」同一事务（``app.db.outbox``），relay 投到 ``biz/content.events``，
由 search 模块的索引 worker 消费后增量同步外部索引（Meilisearch / OpenSearch）。

**事件是「失效通知」而非数据快照**：payload 只带 ``item_id`` 与动作，消费侧回查业务库取
最新文档。这样天然容忍乱序与重放（永远取最新态），事件体也不随正文增长；代价是消费者要
读业务库（同库，indexer worker 本就持有会话）。

**草稿不发**：仅 ``status == published`` 的落库发 ``published``；软删显式发 ``deleted``
（彼时行仍在、status 未变）；已发布内容的正文/状态变更发 ``updated``。未发布内容不对外
可见，索引侧无需感知。

幂等：不传显式 ``event_id``（与 ``enqueue_points_event`` 同口径），由 outbox 行自身承载；
消费侧按 ``item_id`` 做 upsert/delete，天然可重复执行。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.messaging import (
    RKEY_CONTENT_DELETED,
    RKEY_CONTENT_PUBLISHED,
    RKEY_CONTENT_UPDATED,
)
from app.db.outbox import enqueue_outbox
from app.modules.content.models import ContentItem, ContentStatus

CONTENT_ACTION_PUBLISHED: Final = "published"
CONTENT_ACTION_UPDATED: Final = "updated"
CONTENT_ACTION_DELETED: Final = "deleted"

_ACTION_RKEYS: dict[str, str] = {
    CONTENT_ACTION_PUBLISHED: RKEY_CONTENT_PUBLISHED,
    CONTENT_ACTION_UPDATED: RKEY_CONTENT_UPDATED,
    CONTENT_ACTION_DELETED: RKEY_CONTENT_DELETED,
}

# worker 侧分派函数名（payload["fn"]），handler 注册于 search 模块的 tasks.py
CONTENT_EVENT_FN: Final = "apply_content_event"


async def enqueue_content_event(
    db: AsyncSession, item: ContentItem, action: str | None = None
) -> bool:
    """把内容项的一次可见性变化排进 outbox（与业务**同一事务**，不 commit）。

    ``action`` 缺省按 ``item.status`` 推断：``published`` → 发 ``published``，其余（draft/
    pending/rejected）→ 不发并返回 False。软删路径必须显式传 ``CONTENT_ACTION_DELETED``。

    返回 True=已 join 进事务；False=开关关闭/草稿态/总线未配置（见 ``enqueue_outbox``）。
    """
    if not settings.content_events_enabled:
        return False

    resolved = action
    if resolved is None:
        if item.status != ContentStatus.PUBLISHED:
            return False
        resolved = CONTENT_ACTION_PUBLISHED

    rkey = _ACTION_RKEYS.get(resolved)
    if rkey is None:
        raise ValueError(f"unsupported content action: {resolved!r}")

    return await enqueue_outbox(
        db, rkey, {"fn": CONTENT_EVENT_FN, "args": [item.id, resolved]}
    )
