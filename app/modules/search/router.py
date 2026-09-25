"""search REST：站内检索（只读、匿名可访问）。

引擎由 ``LKM_SEARCH_ENGINE`` 择一：``pg``（P1，tsvector + pg_trgm）/ ``meilisearch``（P2）
/ ``opensearch``（P3）；后两者命中后回查 DB 权威回填，调用失败 fail-open 回落 PG
（见 ``search/service.py`` 模块 docstring）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.common import ApiResp, ModuleStatus, PageData, PaginateDep, PaginateParams
from app.core.config import settings
from app.core.err import respond
from app.core.wire import msgspec_ok
from app.db.session import get_read_session
from app.modules.search.schemas import SearchHit
from app.modules.search.service import search_items
from app.modules.search.wire import to_wire

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/status", response_model=ModuleStatus)
async def search_status() -> ModuleStatus:
    return ModuleStatus(
        module="search",
        status="implemented",
        responsibility="站内检索：已发布内容的只读聚合（PG FTS + pg_trgm；可切 Meilisearch/OpenSearch）。",
        next_steps=[
            "OpenSearch 侧中文分词需集群插件（ik/smartcn），未装则用 standard 分析器",
            "检索结果缓存与 P1 索引调优按收益复评",
        ],
    )


@router.get("", response_model=ApiResp[PageData[SearchHit]])
@respond
async def search(
    q: Annotated[str, Query(min_length=1, max_length=200, description="检索词")],
    content_type: Annotated[str | None, Query(max_length=20)] = None,
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[SearchHit] | Response:
    """检索端点（B6c 扩面）。

    ``response_model`` 仅用于 OpenAPI 文档：开启 ``read_msgspec_enabled`` 时返回已用 msgspec
    预编码的 ``Response``（FastAPI 对 Response 实例直接透传），关闭时返回 Pydantic 走既有
    路径。两条路径 JSON 等价由 ``tests/test_read_msgspec.py`` 的参数化用例守。
    """
    page = await search_items(
        db, q, page=pag.page, limit=pag.limit, content_type=content_type
    )
    if settings.read_msgspec_enabled:
        return msgspec_ok(to_wire(page))
    return page
