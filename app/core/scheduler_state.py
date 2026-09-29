"""调度器运行态的跨进程暴露（蓝图 §5.5 第 6 条「生命周期监控」）。

调度器跑在**独立的 worker-scheduler 进程**（§5.5-3① 的拓扑，见 ``worker_scheduler.py``），
而 Prometheus 只抓 ``backend:8000``（父仓 ``deploy/prometheus/prometheus.yml``）——调度器
进程内的 gauge **没有任何人抓**，直接 set 等于自娱自乐。故运行态经 **Redis 心跳**跨进程暴露，
与 ``pulsar_lag`` 同一范式（「API 进程替非 API 进程取数」）：

- **调度器进程**周期性把 ``{state, jobs, pending}`` 写进 ``scheduler:heartbeat``，TTL = 3×周期；
  起停与每次 cron 触发都即时刷新一次，不必等下一拍。
- **API 进程**的 reporter 周期读取并 set 到 gauge。**键不存在**（进程没了 / 卡死 / 收尾后未再
  续写）→ ``scheduler_up=0``——这正是蓝图要的「生命周期异常（未停残余）可观测」：收尾时最后
  一拍会带上 ``state=0`` 与残余在途数，随后 TTL 到期转 up=0。

Redis 不可用时两侧都 fail-open：写失败只记日志、读失败保持上次值并把 ``scheduler_up`` 置 0。
后者在 Redis 故障时会误报「调度器 down」，但 Redis 是硬依赖（readiness 会先红），且「不知道
调度器状态」与「调度器异常」对告警而言同解——宁可吵，不可沉默。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.core.metrics import (
    scheduler_jobs,
    scheduler_pending_jobs,
    scheduler_state,
    scheduler_up,
)
from app.core.redis import get_redis

logger = logging.getLogger("lkm.scheduler_state")

HEARTBEAT_KEY = "scheduler:heartbeat"

# TTL 取周期的 3 倍：允许漏两拍（写失败/短暂不可用）而不误判 down，同时又能在一个合理
# 窗口内发现「进程真的没了」。不单列配置项——它没有独立自由度，随周期缩放即可。
_TTL_MULTIPLIER = 3

# —— 调度器进程内的运行态（唯一写者：worker-scheduler 进程）——
_jobs = 0
_state = 0  # 1=运行中，0=已暂停/已停
_pending = 0  # 在途 cron 作业数


def note_started(jobs: int) -> None:
    """调度器已启动（``jobs`` = 注册的 cron 作业数）。"""
    global _state, _jobs
    _state, _jobs = 1, jobs


def note_stopped() -> None:
    """调度器已暂停/停止（在途数保留，作为「未停残余」上报）。"""
    global _state
    _state = 0


def note_job_started() -> None:
    global _pending
    _pending += 1


def note_job_finished() -> None:
    global _pending
    _pending = max(0, _pending - 1)


def snapshot() -> dict[str, int]:
    """当前运行态快照（心跳载荷）。"""
    return {"state": _state, "jobs": _jobs, "pending": _pending}


async def write_heartbeat(
    redis: Any | None = None, *, interval_s: float | None = None
) -> bool:
    """把快照写进 Redis 心跳（带 TTL）；返回是否写成功。Redis 不可用 → False（fail-open）。"""
    from app.core.config import settings

    period = settings.scheduler_heartbeat_interval_s if interval_s is None else interval_s
    client = redis if redis is not None else await get_redis()
    if client is None:
        return False
    try:
        await client.set(
            HEARTBEAT_KEY,
            json.dumps(snapshot()),
            ex=max(1, int(period * _TTL_MULTIPLIER)),
        )
        return True
    except Exception:
        logger.warning("调度器心跳写入失败（fail-open）", exc_info=True)
        return False


async def run_heartbeat(interval_s: float | None = None) -> None:
    """调度器进程的心跳循环（由调用方 cancel 收尾，与 ``pulsar_lag._run`` 同款）。"""
    from app.core.config import settings

    period = (
        settings.scheduler_heartbeat_interval_s if interval_s is None else interval_s
    )
    period = max(period, 1.0)  # 下界 1s：配成 0/负数会退化成紧凑轮询
    while True:
        await write_heartbeat(interval_s=period)
        await asyncio.sleep(period)


# ---- API 进程侧：读心跳 → set gauge ----


async def collect_once(redis: Any | None = None) -> None:
    """读一次心跳并更新 gauge（API 进程调用）。"""
    client = redis if redis is not None else await get_redis()
    raw: Any = None
    if client is not None:
        try:
            raw = await client.get(HEARTBEAT_KEY)
        except Exception:
            logger.warning("调度器心跳读取失败（置 up=0）", exc_info=True)
            raw = None
    payload: dict[str, Any] | None = None
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                payload = decoded
        except (TypeError, ValueError):
            logger.warning("调度器心跳载荷损坏，按 down 处理")
    if payload is None:
        # 键不存在/损坏：进程没了、卡死、或 Redis 断——都按「不可认为在跑」处置
        scheduler_up.set(0)
        scheduler_state.set(0)
        return
    scheduler_up.set(1)
    scheduler_state.set(1 if payload.get("state") else 0)
    scheduler_jobs.set(float(payload.get("jobs", 0)))
    scheduler_pending_jobs.set(float(payload.get("pending", 0)))


_task: asyncio.Task[None] | None = None


async def _run_reporter() -> None:
    from app.core.config import settings

    # 下界 1s，理由同 run_heartbeat
    period = max(settings.scheduler_heartbeat_interval_s, 1.0)
    while True:
        try:
            await collect_once()
        except Exception:
            # 轮级兜底：任一轮异常不得让 reporter 永久停更（否则指标静默冻结在旧值）
            logger.exception("调度器运行态上报轮次异常，跳过本轮")
        await asyncio.sleep(period)


def start_reporter() -> None:
    """在 API 进程启动运行态上报（幂等；Redis 未配置则整体空转，见 collect_once 的 fail-open）。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run_reporter())
        logger.info("调度器运行态上报已启动")


async def stop_reporter() -> None:
    """取消上报任务（幂等/可重复调用）。"""
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("调度器运行态上报任务此前已异常退出", exc_info=True)
