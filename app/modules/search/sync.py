"""``content.*`` 事件 → 外部索引增量同步（B2）。

**事件是失效通知，不是数据快照**：本层回查当前行后决定 upsert 还是 delete，因此对乱序与
重放天然安全（重复执行同一事件得到同一结果），也不需要事件携带正文。

可见性口径与检索读路径一致：仅 ``status=published`` 且未软删的行进索引；其余（草稿、待审、
下架、已删）一律从索引删除——**索引里不该存在任何不可见内容**，避免索引滞后导致泄漏。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import new_worker_session as new_session
from app.modules.content.models import ContentStatus
from app.modules.search.documents import build_doc
from app.modules.search.engines.base import SearchEngine
from app.modules.search.engines.factory import get_engine
from app.modules.search.repository import SearchRepository
from auth.snapshot import get_user_snapshot_batch

logger = logging.getLogger(__name__)


async def _author_name(db: AsyncSession, author_id: uuid.UUID | None) -> str:
    if author_id is None:
        return ""
    snaps = await get_user_snapshot_batch(db, user_ids=[author_id])
    snap = snaps.get(author_id)
    return snap.display_name if snap else ""


async def sync_item(db: AsyncSession, engine: SearchEngine, item_id: uuid.UUID) -> str:
    """把一条内容行同步到引擎，返回实际动作（``upsert`` / ``delete``）。"""
    item = await SearchRepository(db).get(item_id, include_deleted=True)
    if (
        item is None
        or item.deleted_at is not None
        or item.status != ContentStatus.PUBLISHED
    ):
        await engine.delete([str(item_id)])
        return "delete"
    await engine.upsert([build_doc(item, await _author_name(db, item.author_id))])
    return "upsert"


async def apply_content_event(item_id: str, action: str) -> None:
    """worker handler 的落点：消费一次 ``content.*`` 事件。

    引擎未启用（``pg``）或同步开关关闭 → 直接返回（只由调用方记账）。构造失败（配置错）
    同样是 no-op；只有**引擎调用**异常才向上抛，交由 worker 负确认重投（幂等可重跑）。
    """
    engine = get_engine()
    if engine is None or not settings.search_sync_enabled:
        return
    try:
        uid = uuid.UUID(str(item_id))
    except ValueError:
        logger.warning(
            "content 事件 item_id 非 uuid，丢弃 item=%r action=%s", item_id, action
        )
        return

    db = await new_session()
    try:
        await sync_item(db, engine, uid)
    finally:
        await db.close()
