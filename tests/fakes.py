"""测试替身：内存消息总线 transport 等。

默认测试套件不依赖真实 Pulsar。经 ``messaging.set_transport(InMemoryTransport())`` 注入后，
``messaging.publish`` 走内存记录而非 broker，可直接断言发布的 topic/属性/负载。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: 水位查询里表名必须是**裸标识符**：带库限定（db.t）、引号/反引号包裹的变体原先会被
#: 当成另一个 key，watermark 取不到 → 静默返回「空表」（伪装成首次全量导出）。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class QueryResult:
    """ClickHouse 查询结果替身（结构匹配官方 QueryResult 的 result_rows/column_names）。"""

    result_rows: list[tuple[Any, ...]] = field(default_factory=list)
    column_names: list[str] = field(default_factory=list)


class FakeClickHouseClient:
    """内存 ClickHouse 客户端替身（M5 7.2.6 测试用）。

    - 记录全部 ``query``/``insert`` 调用（断言命令数与参数化）；
    - ``watermarks`` 维护各表 ``max(id)``（uuid7 字符串，字典序即时间序），``insert`` 时
      按 ``id`` 列推进，测「重跑 diff=0」；
    - ``fail_insert=True`` 时 insert 抛错，测失败不被静默吞；
    - admin 查询用 ``count``/``rows``/``columns`` 预设返回值。
    """

    def __init__(
        self,
        *,
        count: int = 0,
        rows: list[tuple[Any, ...]] | None = None,
        columns: list[str] | None = None,
        watermarks: dict[str, str] | None = None,
        fail_insert: bool = False,
    ) -> None:
        self.count = count
        self.rows = rows or []
        self.columns = columns or []
        self.watermarks = dict(watermarks or {})
        self.fail_insert = fail_insert
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[tuple[Any, ...]], list[str]]] = []
        self.commands: list[str] = []
        self.closed = False

    def _ensure_open(self) -> None:
        # closed 原先只是个没人检查的标志：close() 之后 query/insert 照常工作，会给出
        # 「关掉还能用」的假信心。
        if self.closed:
            raise RuntimeError("FakeClickHouseClient 已关闭，不应再收到查询/写入")

    async def query(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> QueryResult:
        self._ensure_open()
        self.queries.append((sql, dict(parameters or {})))
        # 先归一（去首尾空白与结尾分号）再分派：三引号/带分号的写法原先匹配不上，
        # 会掉进下面的 rows 兜底、被当成「空表」而看不出是分派失败。
        stmt = sql.strip().rstrip(";").strip()
        if stmt.startswith("SELECT max(id) FROM"):
            table = stmt[len("SELECT max(id) FROM") :].strip()
            if not _IDENT_RE.fullmatch(table):
                raise AssertionError(f"无法解析水位查询的表名：{sql!r}")
            return QueryResult(
                result_rows=[(self.watermarks.get(table),)], column_names=["max(id)"]
            )
        if stmt.startswith("SELECT count()"):
            return QueryResult(result_rows=[(self.count,)], column_names=["count()"])
        return QueryResult(result_rows=list(self.rows), column_names=list(self.columns))

    async def insert(
        self,
        table: str,
        data: list[tuple[Any, ...]],
        column_names: list[str],
    ) -> None:
        self._ensure_open()
        if self.fail_insert:
            raise RuntimeError("fake clickhouse insert failure")
        self.inserts.append((table, list(data), list(column_names)))
        # 水位只按 id 列推进，且假定 id 是 uuid7（字符串字典序==时间序）。缺列时原先静默
        # 不推进，「重跑 diff=0」类断言会因错误的原因通过/失败，这里显式报错。
        if "id" not in column_names:
            raise ValueError(
                f"FakeClickHouseClient 依赖 id 列推进水位，表 {table!r} 的列 "
                f"{list(column_names)!r} 中没有 'id'"
            )
        i = list(column_names).index("id")
        for row in data:
            value = str(row[i])
            current = self.watermarks.get(table)
            # uuid7 字符串字典序 == 时间序，故 max() 即「最新已导出行」。
            self.watermarks[table] = value if current is None else max(current, value)

    async def command(self, sql: str) -> None:
        self._ensure_open()
        # 原先直接 return None 丢掉 SQL：测试无法断言 DDL/命令类调用
        self.commands.append(sql)

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
        """已发布消息的 JSON 负载列表。

        空/非 JSON 负载通常说明该用例在验证裸传输契约（或发布方写坏了）：给出带 topic 与
        原始字节的明确断言信息，而不是从 json.loads 冒一个无上下文的 JSONDecodeError。
        """
        out: list[dict[str, Any]] = []
        for topic, data, _ in self.published:
            try:
                out.append(json.loads(data))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise AssertionError(
                    f"topic={topic!r} 的已发布负载不是合法 JSON"
                    f"（{len(data)}B）：{data[:80]!r}"
                ) from exc
        return out

    def clear(self) -> None:
        self.published.clear()
