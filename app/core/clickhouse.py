"""ClickHouse 分析管道客户端（M5 7.2.6）。

蓝图《后端规划.md》§6.2：ClickHouse 承担**日志存储 / 分析 / 检索**，与 TimescaleDB
连续聚合的「计数/度量聚合」并行互补。本模块只提供客户端基座，不承载业务 SQL——业务侧
导出入口在 owner 域（``app/db/event_failure_export.py``、``app/modules/auth/audit_export.py``），
查询入口在 admin 只读 port。

设计要点（对齐 ``core.tracing`` 的 fail-open 范式）：
- **默认关闭**（``settings.clickhouse_enabled=false``）：不建连接、零依赖零副作用。
- **测试 seam**：``set_client_factory`` 注入 fake client（内存记录 query/insert、模拟水位）；
  未注入时按 ``settings.clickhouse_url`` 懒建官方 async client。
- **连接懒建单例**：首个调用方建连，``close`` 幂等释放。
- 客户端不可用/未启用时 ``get_client`` 抛 :class:`ClickHouseUnavailableError`，由调用方
  决定语义（导出侧重试、查询接口转 503）——绝不静默返回空数据造成假绿。
"""

from __future__ import annotations

import datetime
import inspect
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any, Protocol
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)


class ClickHouseUnavailableError(RuntimeError):
    """ClickHouse 未启用或连接不可用（调用方据此决定重试 / 503）。"""


class ClickHouseClient(Protocol):
    """导出/查询所需的最小客户端契约（官方 client 与测试 fake 共同满足）。"""

    async def query(self, sql: str, parameters: Any = None) -> Any: ...

    async def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
    ) -> Any: ...

    async def command(self, sql: str) -> Any: ...


# 测试 seam：注入 fake 客户端工厂（同步或 async 返回均可）
_client_factory: Callable[[], Any] | None = None
_client: Any = None


def set_client_factory(factory: Callable[[], Any] | None) -> None:
    """注入/清除客户端工厂（测试用；None 恢复官方懒建路径并丢弃现有单例）。"""
    global _client_factory, _client
    _client_factory = factory
    _client = None


def is_enabled() -> bool:
    """是否配置了可用的分析后端（默认关：未启用则不建连接、导出 no-op）。"""
    return bool(settings.clickhouse_enabled and settings.clickhouse_url)


def _parse_target(url: str) -> dict[str, Any]:
    """从 ``http(s)://host:port`` 解析 clickhouse-connect 建连参数。"""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or (8443 if parsed.scheme == "https" else 8123),
        "username": settings.clickhouse_user,
        "password": settings.clickhouse_password.get_secret_value(),
        "database": settings.clickhouse_database,
        "secure": parsed.scheme == "https",
    }


async def _default_factory() -> ClickHouseClient:
    """按配置建官方 async client（延迟 import，测试无依赖也可加载本模块）。"""
    import clickhouse_connect

    return await clickhouse_connect.get_async_client(
        **_parse_target(settings.clickhouse_url)
    )


async def get_client() -> ClickHouseClient:
    """取/建懒加载单例客户端；未启用或建连失败时抛 ClickHouseUnavailableError。"""
    global _client
    if _client is not None:
        return _client
    if not is_enabled():
        raise ClickHouseUnavailableError(
            "ClickHouse 未启用（LKM_CLICKHOUSE_ENABLED/URL）"
        )
    factory = _client_factory or _default_factory
    try:
        client = factory()
        if inspect.isawaitable(client):
            client = await client
    except Exception as exc:
        logger.exception("ClickHouse 客户端建连失败")
        raise ClickHouseUnavailableError("ClickHouse 连接不可用") from exc
    _client = client
    logger.info("ClickHouse 客户端已连接 database=%s", settings.clickhouse_database)
    return _client


async def close() -> None:
    """幂等释放单例连接（应用 shutdown / 测试复位）。"""
    global _client
    client = _client
    _client = None
    if client is None:
        return
    with suppress(Exception):
        result = client.close()
        if inspect.isawaitable(result):
            await result


# ---- 导出/查询共用小工具（纯函数，无连接状态）----


def result_rows(result: Any) -> list[tuple[Any, ...]]:
    """从官方 QueryResult 或测试 fake（直接返回 list）取行列表。"""
    rows = getattr(result, "result_rows", result)
    return list(rows or [])


async def fetch_watermark(client: ClickHouseClient, table: str) -> str | None:
    """取 CH 表当前最大业务 id 作增量水位；空表返回 ``None``（调用方据此首次全量导出）。

    业务 id 为 uuid7，CH 列类型 String：其字符串字典序与时间序一致，故 ``max(id)``
    仍是最新已导出行。水位以字符串形式返回，PG 侧 ``Uuid`` 列与之直接比较
    （SQLAlchemy 原生 uuid 绑定，asyncpg 接受十六进制字符串；见 export 单测）。

    ``table`` 只接受代码内常量（导出口径表名），不接受外部输入——SQL 以 f-string 拼接表名，
    绝不拼接任何用户可控标识符。
    """
    rows = result_rows(await client.query(f"SELECT max(id) FROM {table}"))
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0])


def to_ch_datetime(value: datetime.datetime) -> datetime.datetime:
    """规范化成 ClickHouse DateTime64 友好的 naive UTC（CH 容器默认 UTC 时区）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.UTC).replace(tzinfo=None)
