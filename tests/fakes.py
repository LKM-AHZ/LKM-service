"""测试替身：内存消息总线 transport 等。

默认测试套件不依赖真实 Pulsar。经 ``messaging.set_transport(InMemoryTransport())`` 注入后，
``messaging.publish`` 走内存记录而非 broker，可直接断言发布的 topic/属性/负载。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """ClickHouse 查询结果替身（结构匹配官方 QueryResult 的 result_rows/column_names）。"""

    result_rows: list[tuple[Any, ...]] = field(default_factory=list)
    column_names: list[str] = field(default_factory=list)


class FakeClickHouseClient:
    """内存 ClickHouse 客户端替身（M5 7.2.6 测试用）。

    - 记录全部 ``query``/``insert`` 调用（断言命令数与参数化）；
    - ``watermarks`` 维护各表 ``max(id)``，``insert`` 时按 ``id`` 列推进，测「重跑 diff=0」；
    - ``fail_insert=True`` 时 insert 抛错，测失败不被静默吞；
    - admin 查询用 ``count``/``rows``/``columns`` 预设返回值。
    """

    def __init__(
        self,
        *,
        count: int = 0,
        rows: list[tuple[Any, ...]] | None = None,
        columns: list[str] | None = None,
        watermarks: dict[str, int] | None = None,
        fail_insert: bool = False,
    ) -> None:
        self.count = count
        self.rows = rows or []
        self.columns = columns or []
        self.watermarks = dict(watermarks or {})
        self.fail_insert = fail_insert
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[tuple[Any, ...]], list[str]]] = []
        self.closed = False

    async def query(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> QueryResult:
        self.queries.append((sql, dict(parameters or {})))
        if sql.startswith("SELECT max(id) FROM "):
            table = sql[len("SELECT max(id) FROM ") :].strip()
            return QueryResult(
                result_rows=[(self.watermarks.get(table),)], column_names=["max(id)"]
            )
        if sql.startswith("SELECT count()"):
            return QueryResult(result_rows=[(self.count,)], column_names=["count()"])
        return QueryResult(result_rows=list(self.rows), column_names=list(self.columns))

    async def insert(
        self,
        table: str,
        data: list[tuple[Any, ...]],
        column_names: list[str],
    ) -> None:
        if self.fail_insert:
            raise RuntimeError("fake clickhouse insert failure")
        self.inserts.append((table, list(data), list(column_names)))
        if "id" in column_names:
            i = list(column_names).index("id")
            for row in data:
                self.watermarks[table] = max(self.watermarks.get(table, 0), int(row[i]))

    async def command(self, sql: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def inserted_rows(self) -> int:
        return sum(len(data) for _, data, _ in self.inserts)


class InMemoryTransport:
    """记录发布的内存 transport（结构匹配 messaging.Transport 协议）。

    ``fail=True`` 时 ``publish`` 抛错，用于验证 fail-open 与失败计数。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.fail = fail

    async def publish(self, topic: str, data: bytes, props: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("in-memory transport failure")
        self.published.append((topic, data, props))

    def payloads(self) -> list[dict[str, Any]]:
        """已发布消息的 JSON 负载列表。"""
        return [json.loads(data) for _, data, _ in self.published]

    def clear(self) -> None:
        self.published.clear()
