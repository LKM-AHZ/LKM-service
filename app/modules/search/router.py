"""search REST：站内检索（只读、匿名可访问）。

P1 只做 PG 原生检索（tsvector + pg_trgm），P2/P3（Meilisearch/OpenSearch）判据见
蓝图 §6.5.3，未达不实施。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp, ModuleStatus, PageData, PaginateDep, PaginateParams
from app.core.err import respond
from app.db.session import get_read_session
from app.modules.search.schemas import SearchHit
from app.modules.search.service import search_items

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/status", response_model=ModuleStatus)
async def search_status() -> ModuleStatus:
    return ModuleStatus(
        module="search",
        status="implemented",
        responsibility="站内检索：已发布内容的只读聚合（PG FTS + pg_trgm 双路）。",
        next_steps=[
            "P2/P3 外部检索引擎（Meilisearch/OpenSearch）按蓝图 §6.5.3 判据触发",
        ],
    )


@router.get("", response_model=ApiResp[PageData[SearchHit]])
@respond
async def search(
    q: Annotated[str, Query(min_length=1, max_length=200, description="检索词")],
    content_type: Annotated[str | None, Query(max_length=20)] = None,
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[SearchHit]:
    return await search_items(
        db, q, page=pag.page, limit=pag.limit, content_type=content_type
    )
