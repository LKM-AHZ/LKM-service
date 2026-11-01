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
