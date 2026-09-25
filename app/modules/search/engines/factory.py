"""引擎工厂：按 ``settings.search_engine`` 返回实例（``pg`` → ``None``）。

**fail-open 口径**：``pg`` 之外的引擎若配置缺失或构造失败，一律记 ``error`` 并返回 ``None``
——读路径回落 PG、写路径只记账，与 ClickHouse / OTel 等可选外部组件的既定取向一致
（外部组件不可用不拖垮主链路）。返回 ``None`` 的两种含义在日志/metric 上可区分：
``search_engine=pg``（正常，不记日志）vs 配置不完整（记 error）。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import settings
from app.core.secrets import reveal
from app.modules.search.engines.base import SearchEngine
from app.modules.search.engines.meili import MeiliSearchEngine
from app.modules.search.engines.opensearch import OpenSearchEngine

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> SearchEngine | None:
    """当前配置的引擎实例；``pg`` 或配置不完整时为 ``None``。进程内缓存。"""
    kind = settings.search_engine
    if kind == "pg":
        return None

    if kind == "meilisearch":
        if not settings.search_meili_url:
            logger.error(
                "search_engine=meilisearch 但 LKM_SEARCH_MEILI_URL 为空，回落 PG 检索"
            )
            return None
        return MeiliSearchEngine(
            url=settings.search_meili_url,
            api_key=reveal(settings.search_meili_api_key) or "",
            index=settings.search_meili_index,
        )

    if kind == "opensearch":
        if not settings.search_opensearch_url:
            logger.error(
                "search_engine=opensearch 但 LKM_SEARCH_OPENSEARCH_URL 为空，回落 PG 检索"
            )
            return None
        return OpenSearchEngine(
            url=settings.search_opensearch_url,
            user=settings.search_opensearch_user,
            password=reveal(settings.search_opensearch_password) or "",
            index=settings.search_opensearch_index,
        )

    logger.error("未知 search_engine=%r，回落 PG 检索", kind)
    return None


def reset_engine_cache() -> None:
    """清缓存（测试在 monkeypatch settings 后调用；生产无调用点）。"""
    get_engine.cache_clear()
