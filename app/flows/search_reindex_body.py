"""检索索引全量重建的纯体层（B2，**无 Prefect 依赖**）。

被两条路径共用：

- ``app/flows/search_reindex.py`` 的 Prefect task（生产，带重试）；
- CLI（``python -m app.flows.search_reindex``，容器内手工重建）。

刻意不 import prefect：保证 CLI/无 Prefect 场景零依赖、worker 冷启动不被拖累。

**流程 = drop → ensure → 分批 upsert**，不做 alias swap：读路径在引擎不可用时 fail-open
回落 PG，重建窗口内的可用性由 PG 兜底，因此不必承担「双索引并存 / 原子切换」的一致性
复杂度。代价是重建期间外部引擎检索结果为空（PG 兜底），取舍登记 §8。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.db.session import new_session
from app.modules.search.documents import build_doc
from app.modules.search.engines.factory import get_engine
from app.modules.search.repository import SearchRepository
from auth.snapshot import get_user_snapshot_batch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def rebuild_index(
    *,
    batch_size: int | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """重建外部检索索引，返回 ``{engine, indexed}``。

    引擎未启用（``pg``）或配置不完整 → no-op（``engine=None``），不报错：与读路径的
    fail-open 口径一致，避免「没配引擎却跑重建」把数据作业判成失败。

    ``db`` 可注入（测试传同库会话；生产留空则自开自关）——「注入式纯编排」是 flows 的
    既有范式，也让本函数在 database-per-test 的测试库里可跑。
    """
    engine = get_engine()
    if engine is None:
        return {"engine": None, "indexed": 0, "reason": "engine=pg 或配置不完整"}

    size = batch_size if batch_size else settings.search_sync_batch_size
    if size <= 0:
        raise ValueError(f"batch_size 必须为正数，实测 {size}")

    await engine.drop_index()
    await engine.ensure_index()

    owned = db is None
    session = db if db is not None else await new_session()
    indexed = 0
    try:
        repo = SearchRepository(session)
        last_id = uuid.UUID(int=0)
        while True:
            rows = await repo.list_published_batch(after_id=last_id, limit=size)
            if not rows:
                break
            author_ids = {r.author_id for r in rows if r.author_id}
            names: dict[uuid.UUID, str] = {}
            if author_ids:
                snaps = await get_user_snapshot_batch(
                    session, user_ids=list(author_ids)
                )
                names = {uid: s.display_name for uid, s in snaps.items()}
            docs = [
                build_doc(r, names.get(r.author_id, "") if r.author_id else "")
                for r in rows
            ]
            indexed += await engine.upsert(docs)
            last_id = rows[-1].id
    finally:
        if owned:
            await session.close()

    return {"engine": engine.name, "indexed": indexed}
