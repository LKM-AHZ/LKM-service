"""compose worker-scheduler 服务入口：跑 APScheduler（cron 触发投递到消息总线）。"""

import asyncio
import logging
import signal
from contextlib import suppress

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from boot.assemble import assemble
from core import metrics_relay, scheduler_state
from core.scheduler import build_scheduler
from core.tracing import setup_tracing, shutdown_tracing

# 先装配（登记模型/任务/端口）再启动消费：否则 worker 会「未知任务 ack 丢弃」
assemble()

logger = logging.getLogger("lkm.scheduler")

# 收尾等待在途作业的上界（秒）：超时即强停，避免一个长任务把进程收尾无限拖住。
_SHUTDOWN_WAIT_S = 10.0


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


def _pending_job_futures(sched: AsyncIOScheduler) -> list[asyncio.Future]:
    """取调度器当前在途作业的 future（仅未完成的）。

    APScheduler 3.x 的 ``AsyncIOExecutor`` 把协程作业作为 task 挂在私有
    ``_pending_futures`` 上——这是「此刻在跑什么」的唯一入口（无公开 API）。取不到时
    返回空列表，降级为直接 shutdown（不抛、不影响收尾）。

    注：``_executors`` 是 scheduler 的默认执行器别名；本仓 ``build_scheduler`` 未自定义
    执行器，故 alias 恒为 ``"default"``。
    """
    executor = getattr(sched, "_executors", {}).get("default")
    futures = getattr(executor, "_pending_futures", None)
    if not futures:
        return []
    return [f for f in list(futures) if not f.done()]


async def _graceful_shutdown(sched: AsyncIOScheduler) -> None:
    """蓝图 §5.5-4 优雅关闭：先 ``pause()`` 拒新触发，再等当前作业完成或超时强停。

    **与蓝图字面的实况偏差（照实况修正，见报告）**：蓝图写 ``shutdown(wait=True)``，但
    APScheduler 3.x 的 ``AsyncIOExecutor.shutdown()`` **刻意不实现 wait**——它在 shutdown
    时直接 ``cancel()`` 所有在途 future（源码注释原文："There is no way to honor wait=True
    without converting this method into a coroutine method"）。若照字面调用，反而会**取消**
    正在跑的作业，与「等当前作业完成」的意图相反。故这里：

    1. ``pause()``：同步、立即拒新触发（pause 后不再调度任何新 job）；
    2. ``await`` 在途作业 task 直到完成或 ``_SHUTDOWN_WAIT_S`` 超时——作业是同一条事件
       循环上的协程，``await`` 是唯一既能真正等待、又**不阻塞**循环的方式（若用同步阻塞
       调用等它，循环被占住，协程作业永远跑不完 → 死锁）；
    3. 超时即取消残余（``wait_for`` 超时会取消 gather 及其子 task，即强停），最后
       ``shutdown(wait=False)`` 释放调度器。
    """
    with suppress(Exception):
        sched.pause()  # 拒新触发：关闭窗口内不再产生新作业
    scheduler_state.note_stopped()  # 运行态转「已暂停」（§5.5-6 生命周期可观测）
    pending = _pending_job_futures(sched)
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), _SHUTDOWN_WAIT_S
            )
        except TimeoutError:
            logger.warning(
                "scheduler 收尾：%d 个在途作业超时(%.0fs)未完成，强停",
                len(pending),
                _SHUTDOWN_WAIT_S,
            )
        except Exception:
            # gather(return_exceptions=True) 正常不抛；兜底避免收尾路径被意外异常打断
            logger.exception("scheduler 收尾：等待在途作业异常，继续强停")
    # wait=False：AsyncIOExecutor 不支持 wait，残余 task 由其 shutdown 取消收口
    sched.shutdown(wait=False)


async def _main() -> None:
    # 调度进程不是 ASGI app：初始化 provider，让 cron 触发的 span 能导出（默认关时 no-op）
    setup_tracing(service_suffix="-scheduler")

    sched = build_scheduler()
    sched.start()
    scheduler_state.note_started(len(sched.get_jobs()))
    # 运行态心跳（§5.5-6）：本进程不暴露 /metrics，故把状态写进 Redis 交给 API 进程的
    # reporter 上报；TTL 到期即意味着本进程已亡，API 侧 scheduler_up 转 0。
    heartbeat = asyncio.create_task(scheduler_state.run_heartbeat())
    # 跨进程指标中继（选项③）：本进程写 notify_failed_total（cron 发布失败路径）且不暴露
    # /metrics，快照同样交给 API 进程代报。
    metrics_relay.start_publisher()
    logger.info("scheduler started")
    try:
        await _wait_for_shutdown()
    finally:
        # 优雅关闭：先拒新触发，等当前作业完成或超时强停（见 _graceful_shutdown 的实况说明）
        await _graceful_shutdown(sched)
        # 收尾前补最后一拍（state=0 + 残余在途数），让 API 侧立刻看到「已停」而不是等 TTL
        await scheduler_state.write_heartbeat()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        # 指标发布 task 显式收尾（键有 TTL，即便不 cancel 也能收敛，这里只为不留悬挂 task）
        await metrics_relay.stop_publisher()
        # setup_tracing 装的是 BatchSpanProcessor，不显式 flush 会丢掉最后一批 span
        # （cron 触发的 publish 与 httpx 埋点都在内）；幂等，异常仅记日志
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(_main())
