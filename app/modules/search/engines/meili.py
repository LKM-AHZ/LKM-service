"""Meilisearch 引擎实现（蓝图 §6.5.3 的 P2）。

选它的理由：内置中文分词（CJK 按字切分），正好补 P1（PG FTS 的 ``simple`` 分词 + pg_trgm
对 2 字中文退化为全表扫）的短板；单容器、无需插件。

**过滤表达式的注入面**：Meilisearch 的 ``filter`` 是表达式字符串、无参数化，故
``content_type`` 必须先过白名单正则（见 ``_SAFE_FILTER_VALUE``）——它来自用户查询参数。

任务等待：所有写操作返回 ``TaskInfo``，必须 ``wait_for_task`` 才算真正生效；本层统一在
``to_thread`` 内等待完成（超时由 SDK 默认值兜底），使「upsert 返回即索引可见」。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from typing import Any

from app.modules.search.engines.base import IndexDoc

logger = logging.getLogger(__name__)

# Meilisearch filter 是表达式字符串（无参数化），值必须白名单化后才可拼接
_SAFE_FILTER_VALUE = re.compile(r"^[a-z0-9_]{1,20}$")

# 可检索/可过滤字段设置：searchable 决定全文匹配面，filterable 决定能按 content_type 过滤
_INDEX_SETTINGS: dict[str, Any] = {
    "searchableAttributes": [
        "title",
        "excerpt",
        "content",
        "summary",
        "tags",
        "keywords",
    ],
    "filterableAttributes": ["content_type", "board_id", "author_id", "tags"],
    "sortableAttributes": ["published_at", "created_at"],
    "rankingRules": [
        "words",
        "typo",
        "proximity",
        "attribute",
        "sort",
        "exactness",
        "published_at:desc",
    ],
}

_TASK_TIMEOUT_MS = 30_000


class MeiliSearchEngine:
    """Meilisearch 客户端封装（懒建连接，避免 import 期触网）。"""

    name = "meilisearch"

    def __init__(self, url: str, api_key: str, index: str) -> None:
        self._url = url
        self._api_key = api_key
        self._index = index
        self._client: Any = None

    def _client_or_create(self) -> Any:
        if self._client is None:
            import meilisearch

            self._client = meilisearch.Client(self._url, self._api_key or None)
        return self._client

    def _wait(self, task: Any) -> None:
        self._client_or_create().wait_for_task(
            task.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS
        )

    async def ensure_index(self) -> None:
        await asyncio.to_thread(self._ensure_sync)

    def _ensure_sync(self) -> None:
        client = self._client_or_create()
        try:
            self._wait(client.create_index(self._index, {"primaryKey": "id"}))
        except Exception as exc:  # 已存在：Meilisearch 返回 index_already_exists
            if "already_exists" not in str(exc):
                raise
        self._wait(client.index(self._index).update_settings(_INDEX_SETTINGS))

    async def drop_index(self) -> None:
        await asyncio.to_thread(self._drop_sync)

    def _drop_sync(self) -> None:
        client = self._client_or_create()
        try:
            self._wait(client.delete_index(self._index))
        except Exception as exc:  # 不存在：index_not_found —— 幂等
            if "not_found" not in str(exc):
                raise

    async def upsert(self, docs: Sequence[IndexDoc]) -> int:
        if not docs:
            return 0
        return await asyncio.to_thread(self._upsert_sync, list(docs))

    def _upsert_sync(self, docs: list[IndexDoc]) -> int:
        client = self._client_or_create()
        self._wait(client.index(self._index).add_documents(docs))
        return len(docs)

    async def delete(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        return await asyncio.to_thread(self._delete_sync, list(ids))

    def _delete_sync(self, ids: list[str]) -> int:
        client = self._client_or_create()
        self._wait(client.index(self._index).delete_documents(ids))
        return len(ids)

    async def search_ids(
        self,
        term: str,
        *,
        content_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[str], int]:
        return await asyncio.to_thread(
            self._search_sync, term, content_type, offset, limit
        )

    def _search_sync(
        self,
        term: str,
        content_type: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[str], int]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if content_type:
            if not _SAFE_FILTER_VALUE.match(content_type):
                raise ValueError(
                    f"unsafe content_type for meili filter: {content_type!r}"
                )
            params["filter"] = f"content_type = '{content_type}'"
        res = self._client_or_create().index(self._index).search(term, params)
        hits = res.get("hits") or []
        ids = [str(hit["id"]) for hit in hits if hit.get("id") is not None]
        total = res.get("estimatedTotalHits")
        return ids, int(total) if total is not None else len(ids)
