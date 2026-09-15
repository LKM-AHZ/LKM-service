"""ClickHouse 客户端基座测试（M5 7.2.6）。

seam 注入 fake 工厂，不依赖真实 ClickHouse：验证开关短路、懒建单例、建连失败 fail-open
语义（抛 ClickHouseUnavailableError 而非静默）、close 幂等。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core import clickhouse
from app.core.clickhouse import ClickHouseUnavailableError
from app.core.config import settings
from tests.fakes import FakeClickHouseClient


@pytest.fixture(autouse=True)
def _reset_clickhouse() -> Iterator[None]:
    clickhouse.set_client_factory(None)
    yield
    clickhouse.set_client_factory(None)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clickhouse_enabled", True)
    monkeypatch.setattr(settings, "clickhouse_url", "http://clickhouse:8123")


async def should_raise_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clickhouse_enabled", False)
    monkeypatch.setattr(settings, "clickhouse_url", "")
    assert clickhouse.is_enabled() is False
    with pytest.raises(ClickHouseUnavailableError):
        await clickhouse.get_client()


async def should_lazy_build_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClickHouseClient()
    calls = 0

    def factory() -> FakeClickHouseClient:
        nonlocal calls
        calls += 1
        return fake

    clickhouse.set_client_factory(factory)
    _enable(monkeypatch)

    first = await clickhouse.get_client()
    second = await clickhouse.get_client()
    assert first is second
    assert calls == 1


async def should_support_async_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClickHouseClient()

    async def factory() -> FakeClickHouseClient:
        return fake

    clickhouse.set_client_factory(factory)
    _enable(monkeypatch)
    assert await clickhouse.get_client() is fake


async def should_wrap_factory_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> object:
        raise RuntimeError("connect refused")

    clickhouse.set_client_factory(boom)
    _enable(monkeypatch)
    with pytest.raises(ClickHouseUnavailableError):
        await clickhouse.get_client()


async def should_close_idempotently(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClickHouseClient()
    clickhouse.set_client_factory(lambda: fake)
    _enable(monkeypatch)

    await clickhouse.get_client()
    await clickhouse.close()
    assert fake.closed is True
    # 二次 close 无客户端可关，幂等不抛
    await clickhouse.close()
