"""compose worker-scheduler 服务入口：跑 APScheduler（cron 触发投递到消息总线）。"""

import asyncio
import logging
import signal
from contextlib import suppress

from app.core.scheduler import build_scheduler
from app.core.tracing import setup_tracing, shutdown_tracing

logger = logging.getLogger("lkm.scheduler")


async def _wait_for_shutdown() -> None:
    """等 SIGINT/SIGTERM 到来。

    容器编排停止进程发的是 SIGTERM，默认行为是立即终止——那会绕过下面的 finally，
    既不做 ``sched.shutdown()``（在跑的 job 被丢），也不 flush tracing。改由信号置事件、
    协程正常醒来走收尾路径。不支持 add_signal_handler 的平台/线程退回默认行为。
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def _main() -> None:
    # 调度进程不是 ASGI app：初始化 provider，让 cron 触发的 span 能导出（默认关时 no-op）
    setup_tracing(service_suffix="-scheduler")

    sched = build_scheduler()
    sched.start()
    logger.info("scheduler started")
    try:
        await _wait_for_shutdown()
    finally:
        sched.shutdown(wait=False)
        # setup_tracing 装的是 BatchSpanProcessor，不显式 flush 会丢掉最后一批 span
        # （cron 触发的 publish 与 httpx 埋点都在内）；幂等，异常仅记日志
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(_main())
