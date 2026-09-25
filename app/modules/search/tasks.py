"""search 模块消费任务：``content.*`` 事件 → 外部检索索引增量同步。

订阅 ``content-index``（``core.messaging.SUB_CONTENT_INDEX``，topic ``biz/content.events``）。

本文件只做**分派与记账**：真正的同步语义（回查行 → upsert/delete）在 ``search/sync.py``。
引擎调用异常向上抛，让 worker 负确认重投（事件可重放、同步幂等）。
"""

import logging

from app.core.messaging import SUB_CONTENT_INDEX
from app.core.metrics import content_index_events_total
from app.core.task_registry import register_task
from app.modules.search import sync

logger = logging.getLogger(__name__)


async def apply_content_event(item_id: str, action: str) -> None:
    """消费一次 ``content.*`` 事件：把内容项增量同步到外部索引。

    ``item_id`` 经事件链路（outbox JSONB → Pulsar JSON）送达时是**字符串**，handler 内不得
    调用 UUID 专有属性（见 ``app/db/outbox.py`` 的消费侧契约）。
    """
    content_index_events_total.labels(action).inc()
    await sync.apply_content_event(item_id, action)


register_task(SUB_CONTENT_INDEX.name, "apply_content_event", apply_content_event)
