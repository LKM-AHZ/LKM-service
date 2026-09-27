"""连接池水位 Collector 单测（蓝图 §3.3 第 3 条）。

覆盖三点：两池各状态采样正确；惰性未创建的池被跳过且**不触发建池**；注册幂等
（重复调用 / 重复 create_app 不得二次注册）。
"""

from __future__ import annotations

from typing import Any

from prometheus_client.registry import REGISTRY

import app.core.metrics as metrics
import app.db.session as session_mod


class _FakePool:
    def __init__(
        self, *, size: int, checked_out: int, overflow: int, checked_in: int
    ) -> None:
        self._size = size
        self._checked_out = checked_out
        self._overflow = overflow
        self._checked_in = checked_in

    def size(self) -> int:
        return self._size

    def checkedout(self) -> int:
        return self._checked_out

    def overflow(self) -> int:
        return self._overflow

    def checkedin(self) -> int:
        return self._checked_in


class _FakeEngine:
    def __init__(self, pool: Any) -> None:
        self.pool = pool


def _samples(collector: metrics.DBPoolCollector) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for family in collector.collect():
        for sample in family.samples:
            out[(sample.labels["pool"], sample.labels["state"])] = sample.value
    return out


class TestCollect:
    def should_sample_both_pools(self, monkeypatch) -> None:
        monkeypatch.setattr(
            session_mod,
            "_async_engine",
            _FakeEngine(_FakePool(size=10, checked_out=4, overflow=2, checked_in=8)),
        )
        monkeypatch.setattr(
            session_mod,
            "_worker_engine",
            _FakeEngine(_FakePool(size=5, checked_out=1, overflow=0, checked_in=4)),
        )

        got = _samples(metrics.DBPoolCollector())

        assert got[("web", "size")] == 10
        assert got[("web", "checked_out")] == 4
        assert got[("web", "overflow")] == 2
        assert got[("web", "checked_in")] == 8
        assert got[("worker", "size")] == 5
        assert got[("worker", "checked_out")] == 1

    def should_skip_lazy_pools_without_creating_them(self, monkeypatch) -> None:
        """未创建的池跳过即可；抓取路径上**不得**调访问器（那会凭空建池开连接）。"""
        monkeypatch.setattr(session_mod, "_async_engine", None)
        monkeypatch.setattr(session_mod, "_worker_engine", None)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("采集时不得触发建池")

        monkeypatch.setattr(session_mod, "get_async_engine", _boom)
        monkeypatch.setattr(session_mod, "get_worker_engine", _boom)

        assert _samples(metrics.DBPoolCollector()) == {}

    def should_skip_engine_without_pool_attribute(self, monkeypatch) -> None:
        monkeypatch.setattr(session_mod, "_async_engine", object())
        monkeypatch.setattr(
            session_mod,
            "_worker_engine",
            _FakeEngine(_FakePool(size=5, checked_out=5, overflow=0, checked_in=0)),
        )

        got = _samples(metrics.DBPoolCollector())

        assert all(pool == "worker" for pool, _ in got), got
        assert got[("worker", "checked_out")] == 5

    def should_not_break_scrape_when_pool_raises(self, monkeypatch) -> None:
        class _BrokenPool:
            def size(self) -> int:
                raise RuntimeError("boom")

            checkedout = checkedin = overflow = size

        monkeypatch.setattr(session_mod, "_async_engine", _FakeEngine(_BrokenPool()))
        monkeypatch.setattr(session_mod, "_worker_engine", None)

        # 单池失败不得让整个 /metrics 抓取 500
        assert _samples(metrics.DBPoolCollector()) == {}


class TestRegistrationIdempotency:
    def should_return_same_instance_and_register_once(self) -> None:
        first = metrics.register_pool_metrics_collector()
        second = metrics.register_pool_metrics_collector()
        assert first is second

        registered = [
            c
            for c in REGISTRY._collector_to_names  # type: ignore[attr-defined]
            if isinstance(c, metrics.DBPoolCollector)
        ]
        assert len(registered) == 1
