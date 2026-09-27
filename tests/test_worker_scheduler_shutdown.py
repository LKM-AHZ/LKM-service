"""蓝图 §5.5-4：scheduler 优雅关闭——先 pause() 拒新触发，再等当前作业完成或超时强停。

用假 scheduler 单测 ``_graceful_shutdown`` 的时序与超时语义；不真起 APScheduler（那需要
消息总线/任务注册全链路）。
"""

from __future__ import annotations

import asyncio

import pytest

import app.core.worker_scheduler as ws


class _FakeExecutor:
    def __init__(self, futures: set[asyncio.Future]) -> None:
        self._pending_futures = futures


class _FakeScheduler:
    """记录 pause/shutdown 调用及次序的假调度器。"""

    def __init__(self, futures: set[asyncio.Future] | None = None) -> None:
        self._executors = {"default": _FakeExecutor(futures or set())}
        self.events: list[str] = []
        self.shutdown_wait: list[bool] = []

    def pause(self) -> None:
        self.events.append("pause")

    def shutdown(self, wait: bool = True) -> None:
        self.events.append("shutdown")
        self.shutdown_wait.append(wait)


class TestPendingFutures:
    async def test_filters_done_futures(self) -> None:
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        pending = asyncio.get_running_loop().create_future()
        sched = _FakeScheduler({done, pending})
        assert ws._pending_job_futures(sched) == [pending]
        pending.cancel()

    def test_missing_executor_is_safe(self) -> None:
        class _Bare:
            pass

        assert ws._pending_job_futures(_Bare()) == []  # type: ignore[arg-type]


class TestGracefulShutdown:
    async def test_pause_then_shutdown_wait_false(self) -> None:
        sched = _FakeScheduler()
        await ws._graceful_shutdown(sched)  # type: ignore[arg-type]
        assert sched.events == ["pause", "shutdown"]
        # AsyncIOExecutor 不支持 wait，必须 wait=False（否则会 cancel 在途作业）
        assert sched.shutdown_wait == [False]

    async def test_waits_for_in_flight_job(self) -> None:
        finished = asyncio.Event()

        async def _job() -> None:
            await asyncio.sleep(0.01)
            finished.set()

        task = asyncio.ensure_future(_job())
        sched = _FakeScheduler({task})
        await ws._graceful_shutdown(sched)  # type: ignore[arg-type]
        assert finished.is_set()  # 先 pause 再等作业跑完，未取消
        assert sched.shutdown_wait == [False]

    async def test_timeout_forces_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _slow_job() -> None:
            await asyncio.sleep(30)

        task = asyncio.ensure_future(_slow_job())
        sched = _FakeScheduler({task})
        monkeypatch.setattr(ws, "_SHUTDOWN_WAIT_S", 0.01)
        await asyncio.wait_for(ws._graceful_shutdown(sched), timeout=2)  # type: ignore[arg-type]
        assert sched.events == ["pause", "shutdown"]
        assert task.cancelled()  # 超时强停：在途作业被取消

    async def test_pause_failure_still_shuts_down(self) -> None:
        class _BadPause(_FakeScheduler):
            def pause(self) -> None:
                raise RuntimeError("pause boom")

        sched = _BadPause()
        await ws._graceful_shutdown(sched)  # type: ignore[arg-type]
        assert sched.events == ["shutdown"]  # pause 异常不阻断收尾
