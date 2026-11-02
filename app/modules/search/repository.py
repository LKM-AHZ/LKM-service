"""检索仓储：只做跨域合并与分页，不持有任何业务域模型。

内容与文件的检索分支、索引文档与可见性口径分别由 ``content.search_api`` /
``files.search_api`` 提供，本模块只经 ``core.ports`` 取用——search 因此不认识任何 ORM。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from core.ports import content as content_port
from core.ports import files as files_port


class SearchRepository:
    """跨域检索的组合层（DB 路径）。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search_with_files(
        self,
        *,
        term: str,
        offset: int,
        limit: int,
        content_type: str | None = None,
    ) -> tuple[int, list[tuple[uuid.UUID, str]]]:
        """公开文件与已发布内容在数据库内合并排序、分页。"""
        content = content_port.search_select(term, content_type)
        files = files_port.search_select(term)
        combined = (
            files.subquery()
            if content_type == "library_file"
            else union_all(content, files).subquery()
        )
        total = await self.db.scalar(select(func.count()).select_from(combined))
        rows = (
            await self.db.execute(
                select(combined.c.id, combined.c.source)
                .order_by(
                    combined.c.rank.desc().nulls_last(),
                    combined.c.created_at.desc(),
                    combined.c.id.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return int(total or 0), [(row.id, row.source) for row in rows]

    async def public_files_by_ids(self, ids: Sequence[uuid.UUID]) -> list[Any]:
        """按 id 批量取公开文件（引擎命中后的权威回填）。"""
        if not ids:
            return []
        result = await self.db.execute(files_port.select_public_by_ids(list(ids)))
        return list(result.scalars().all())

    async def list_published_by_ids(self, ids: Sequence[uuid.UUID]) -> list[Any]:
        """按 id 批量取**已发布**内容（引擎命中后的权威回填）。"""
        if not ids:
            return []
        result = await self.db.execute(content_port.select_published_by_ids(list(ids)))
        return list(result.scalars().all())

    async def count_matching(
        self, *, term: str, content_type: str | None = None
    ) -> int:
        return await content_port.search_count(self.db, term, content_type)

    async def list_matching(
        self,
        *,
        term: str,
        content_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Any]:
        """PG 路径的命中行。"""
        stmt = content_port.select_matching(
            term, content_type, offset=offset, limit=limit
        )
        return list((await self.db.execute(stmt)).scalars().all())
