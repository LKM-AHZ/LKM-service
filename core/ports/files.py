"""files 域只读端口：文件计数与待审聚合。

后台概览与审核摘要只关心计数，不需要 ``LibraryFile`` 实例；状态枚举与「待审」
口径属 files 域，故收进端口。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def count_files(db: AsyncSession, status: str | None = None) -> int:
    """文件总数；给 ``status`` 时只计该状态。"""
    return await get("files_read").count_files(db, status)


async def pending_files(db: AsyncSession) -> tuple[int, Any]:
    """待审文件：``(数量, 最早提交时间)``。"""
    return await get("files_read").pending_files(db)


def search_select(term: str) -> Any:
    """文件侧检索分支：``(id, source, rank, created_at)``，供 search 做 UNION。"""
    return get("files_read").search_select(term)


def select_public_by_ids(ids: list[Any]) -> Any:
    """按 id 批量取公开文件行（未执行）。"""
    return get("files_read").select_public_by_ids(ids)


async def index_doc(db: AsyncSession, file_id: Any) -> Any:
    """可索引则返回索引文档，否则 None。"""
    return await get("files_read").index_doc(db, file_id)


async def index_docs_batch(
    db: AsyncSession, *, after_id: Any, limit: int
) -> Any:
    """按 id 升序取一窗公开文件文档（全量重建索引）。"""
    return await get("files_read").index_docs_batch(db, after_id=after_id, limit=limit)
