"""singleflight：并发合并且引用计数归零回收、异常传播、取消不连带取消共享加载。"""

import asyncio

import pytest

import app.core.singleflight as sf


async def test_concurrent_calls_merge_to_one_loader() -> None:
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 42

    roles: list[str] = []
    results = await asyncio.gather(
        *(sf.run("k", loader, on_role=roles.append) for _ in range(10))
    )
    assert results == [42] * 10
    assert calls == 1
    assert roles.count("leader") == 1
    assert roles.count("shared") == 9
    assert sf.in_flight() == 0  # 引用计数归零即回收，不泄漏


async def test_exception_propagates_to_all_waiters() -> None:
    async def loader() -> int:
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    results = await asyncio.gather(
        *(sf.run("e", loader) for _ in range(3)), return_exceptions=True
    )
    assert all(isinstance(r, ValueError) for r in results)
    assert sf.in_flight() == 0


async def test_owner_cancel_does_not_cancel_shared_load() -> None:
    started = asyncio.Event()

    async def loader() -> str:
        started.set()
        await asyncio.sleep(0.05)
        return "ok"

    owner = asyncio.create_task(sf.run("c", loader))
    await started.wait()
    shared = asyncio.create_task(sf.run("c", loader))
    await asyncio.sleep(0.01)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await shared == "ok"
    assert sf.in_flight() == 0


async def test_sequential_calls_run_loader_each_time() -> None:
    """非并发（前一次已完成并回收）→ 每次各自执行 loader，不错误复用。"""
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await sf.run("s", loader) == 1
    assert await sf.run("s", loader) == 2
    assert sf.in_flight() == 0
