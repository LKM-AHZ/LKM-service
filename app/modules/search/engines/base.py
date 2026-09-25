"""外部检索引擎抽象（B2）：Meilisearch / OpenSearch 共用协议。

分层理由：**本层只做 I/O**。索引文档的构造在 ``search/documents.py``（那里才允许读
content 模型，import-linter 已豁免 search 只读内容表），故本层拿到的是纯 dict，单测可用
假引擎替换，无需起真容器。

同步 SDK 处理：``meilisearch`` / ``opensearch-py`` 都是同步客户端，统一经
``asyncio.to_thread`` 包装——与 ``storage/s3.py``（boto3）同款取法，既不阻塞事件循环，
也不为此引入 async 依赖。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

# 索引文档：字段集对齐蓝图 §6.5.3（id/title/excerpt/content/tags/author/published_at/board）
IndexDoc = Mapping[str, Any]


@runtime_checkable
class SearchEngine(Protocol):
    """外部检索引擎契约：建索引 / 写 / 删 / 查 id 四件事。"""

    name: str

    async def ensure_index(self) -> None:
        """幂等建索引（含 mapping 与过滤字段设置）。"""

    async def drop_index(self) -> None:
        """删索引（全量重建的第一步）；不存在不报错。"""

    async def upsert(self, docs: Sequence[IndexDoc]) -> int:
        """批量写入/更新（按 ``id`` 幂等覆盖），返回提交条数。"""

    async def delete(self, ids: Sequence[str]) -> int:
        """按 ``id`` 删除；不存在的 id 不报错。返回提交条数。"""

    async def search_ids(
        self,
        term: str,
        *,
        content_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[str], int]:
        """检索命中：``(按相关度排序的 id 列表, 命中总数)``。"""
