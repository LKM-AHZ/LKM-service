"""OpenSearch 引擎实现（蓝图 §6.5.3 的 P3）。

面向大规模与聚合场景（P3 判据原文），部署形态是集群而非单容器。

**中文分词**：蓝图要求 ``ik`` / 结巴分析器，但那是**集群侧插件**、不能由应用安装。本实现
用默认 ``standard`` 分析器建 mapping（开箱可用），中文召回质量取决于集群是否装了插件；
mapping 一旦写入，后续补插件需重建索引（走 ``search-reindex`` flow）。此取舍登记 §8。

**证书校验**：自托管常为内网自签证书，故默认 ``verify_certs=False``（与 ClickHouse/OTel
的 fail-open 取向一致）；生产应把集群置于受信网段或前置受信网关。此取舍登记 §8。

写操作统一 ``refresh=True``：保证「upsert 返回即可被搜到」（索引同步的可见性语义），
代价是吞吐——内容索引是低频写、可接受。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from app.modules.search.engines.base import IndexDoc

logger = logging.getLogger(__name__)

# 蓝图 §6.5.3 的索引文档结构 → OpenSearch mapping（tags/keywords 是数组，故 keyword）
_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "content_type": {"type": "keyword"},
            "board_id": {"type": "keyword"},
            "title": {"type": "text"},
            "excerpt": {"type": "text"},
            "content": {"type": "text"},
            "summary": {"type": "text"},
            "tags": {"type": "keyword"},
            "keywords": {"type": "keyword"},
            "author_id": {"type": "keyword"},
            "author_name": {"type": "text"},
            "published_at": {"type": "date"},
            "created_at": {"type": "date"},
        }
    }
}

_SEARCH_FIELDS = ["title^3", "excerpt^2", "content", "summary", "tags", "keywords"]

# 单次 bulk 的条目上限（bulk body 是 2 行/文档，避免请求体过大）
_BULK_CHUNK = 500


class OpenSearchEngine:
    """opensearch-py 客户端封装（懒建连接，避免 import 期触网）。"""

    name = "opensearch"

    def __init__(self, url: str, user: str, password: str, index: str) -> None:
        self._url = url
        self._user = user
        self._password = password
        self._index = index
        self._client: Any = None

    def _client_or_create(self) -> Any:
        if self._client is None:
            from opensearchpy import OpenSearch

            self._client = OpenSearch(
                hosts=[self._url],
                http_auth=(self._user, self._password) if self._user else None,
                use_ssl=self._url.startswith("https://"),
                verify_certs=False,
            )
        return self._client

    async def ensure_index(self) -> None:
        await asyncio.to_thread(self._ensure_sync)

    def _ensure_sync(self) -> None:
        client = self._client_or_create()
        if client.indices.exists(index=self._index):
            return
        try:
            client.indices.create(index=self._index, body=_MAPPING)
        except Exception as exc:  # 并发建索引：resource_already_exists 视为成功
            if "resource_already_exists" not in str(exc):
                raise

    async def drop_index(self) -> None:
        await asyncio.to_thread(self._drop_sync)

    def _drop_sync(self) -> None:
        self._client_or_create().indices.delete(index=self._index, ignore=[404])

    async def upsert(self, docs: Sequence[IndexDoc]) -> int:
        if not docs:
            return 0
        return await asyncio.to_thread(self._upsert_sync, list(docs))

    def _upsert_sync(self, docs: list[IndexDoc]) -> int:
        client = self._client_or_create()
        written = 0
        for start in range(0, len(docs), _BULK_CHUNK):
            chunk = docs[start : start + _BULK_CHUNK]
            body: list[dict[str, Any]] = []
            for doc in chunk:
                body.append({"index": {"_index": self._index, "_id": str(doc["id"])}})
                body.append(dict(doc))
            client.bulk(body=body, refresh=True)
            written += len(chunk)
        return written

    async def delete(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        return await asyncio.to_thread(self._delete_sync, list(ids))

    def _delete_sync(self, ids: list[str]) -> int:
        client = self._client_or_create()
        for start in range(0, len(ids), _BULK_CHUNK):
            chunk = ids[start : start + _BULK_CHUNK]
            body = [{"delete": {"_index": self._index, "_id": str(i)}} for i in chunk]
            # 不存在的 _id 在 bulk 响应里是 404 结果项、不抛错（幂等删除）
            client.bulk(body=body, refresh=True)
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
        must: list[Any] = [{"multi_match": {"query": term, "fields": _SEARCH_FIELDS}}]
        query: dict[str, Any] = {"bool": {"must": must}}
        if content_type:
            # DSL dict 传参，无表达式拼接 → 无注入面
            must.append({"term": {"content_type": content_type}})
        res = self._client_or_create().search(
            index=self._index,
            body={"query": query, "from": offset, "size": limit},
        )
        hits = (res.get("hits") or {}).get("hits") or []
        total = (res.get("hits") or {}).get("total") or {}
        return [str(h["_id"]) for h in hits], int(total.get("value", len(hits)))
