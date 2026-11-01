"""files 域对外只读 API（``core.ports.files`` 的实现去处）。

只提供计数与待办聚合，不把 ``LibraryFile`` 实例交给调用方。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files.models import FileStatus, LibraryFile


async def count_files(db: AsyncSession, status: str | None = None) -> int:
    """文件总数；给 ``status`` 时只计该状态。"""
    stmt = select(func.count(LibraryFile.id))
    if status is not None:
        stmt = stmt.where(LibraryFile.status == status)
    return int(await db.scalar(stmt) or 0)


async def pending_files(
    db: AsyncSession,
) -> tuple[int, datetime.datetime | None]:
    """待审文件：``(数量, 最早提交时间)``。"""
    row: Any = (
        await db.execute(
            select(func.count(LibraryFile.id), func.min(LibraryFile.created_at)).where(
                LibraryFile.status == FileStatus.PENDING
            )
        )
    ).one()
    return int(row[0]), row[1]
