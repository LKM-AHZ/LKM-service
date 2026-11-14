"""插件生命周期钩子：core.lifecycle 的顺序/幂等/失败语义与 boot.plugins 的调用约定。"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from core import lifecycle


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    lifecycle.reset()
    yield
    lifecycle.reset()


async def test_startup_runs_once_and_shutdown_is_lifo() -> None:
    calls: list[str] = []

    async def a_start() -> None:
        calls.append("a+")

    async def a_stop() -> None:
        calls.append("a-")

    async def b_start() -> None:
        calls.append("b+")

    async def b_stop() -> None:
        calls.append("b-")

    lifecycle.register_startup(a_start)
    lifecycle.register_startup(b_start)
    lifecycle.register_shutdown(a_stop)
    lifecycle.register_shutdown(b_stop)

    await lifecycle.run_startup()
    await lifecycle.run_startup()  # 幂等：default worker 多订阅并发进入也只跑一次
    await lifecycle.run_shutdown()

    assert calls == ["a+", "b+", "b-", "a-"]


async def test_shutdown_without_startup_is_noop() -> None:
    called: list[str] = []

    async def stop() -> None:
        called.append("stop")

    lifecycle.register_shutdown(stop)
    await lifecycle.run_shutdown()
    assert called == []


async def test_startup_failure_propagates_and_allows_retry() -> None:
    calls: list[str] = []
    failures = {"n": 1}

    async def flaky() -> None:
        calls.append("start")
        if failures["n"]:
            failures["n"] -= 1
            raise RuntimeError("boom")

    lifecycle.register_startup(flaky)
    with pytest.raises(RuntimeError, match="boom"):
        await lifecycle.run_startup()
    await lifecycle.run_startup()  # 上次失败不置 started，可重试
    assert calls == ["start", "start"]


async def test_shutdown_failure_does_not_block_others() -> None:
    calls: list[str] = []

    async def bad_stop() -> None:
        raise RuntimeError("stop failed")

    async def good_stop() -> None:
        calls.append("good")

    lifecycle.register_shutdown(bad_stop)
    lifecycle.register_shutdown(good_stop)
    await lifecycle.run_startup()
    await lifecycle.run_shutdown()

    assert calls == ["good"]  # LIFO：good 先跑；bad 的异常被吞掉


async def test_boot_plugins_calls_sync_and_async_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    module = ModuleType("fake_plugin_hooks")
    module.plugin_startup = lambda: calls.append("start")  # 同步钩子

    async def _stop() -> None:
        calls.append("stop")

    module.plugin_shutdown = _stop
    monkeypatch.setitem(sys.modules, "fake_plugin_hooks", module)
    monkeypatch.setattr(
        "app.modules.spec.SPECS",
        (SimpleNamespace(name="fake", module_path="fake_plugin_hooks"),),
    )

    from boot import plugins

    await plugins._startup_all()
    await plugins._shutdown_all()
    assert calls == ["start", "stop"]


async def test_boot_plugins_startup_failure_names_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("fake_broken_hooks")

    def _boom() -> None:
        raise ValueError("bad hook")

    module.plugin_startup = _boom
    monkeypatch.setitem(sys.modules, "fake_broken_hooks", module)
    monkeypatch.setattr(
        "app.modules.spec.SPECS",
        (SimpleNamespace(name="broken", module_path="fake_broken_hooks"),),
    )

    from boot import plugins

    with pytest.raises(RuntimeError, match=r"插件 'broken'.*plugin_startup 失败"):
        await plugins._startup_all()
