"""Prefect flow：ClickHouse 分析导出（M5 7.2.6 路 A）。

周期把业务库 ``event_failures`` + auth 库 ``audit_logs`` 增量导出到 ClickHouse。复用
owner 侧入口（``app/db/event_failure_export.py``、``auth/audit_export.py``），
不复制业务 SQL；与 ``app/flows/user_dim.py`` 同构：纯体层 → Prefect task → 注入式纯编排。

两路独立：各自开各自 realm 的会话与 client，一路失败由 task 重试/标记，不影响另一路。

CLI（在 prefect-worker / worker 容器内手工补导）::

    python -m app.flows.analytics --window 1000
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from prefect import flow, task

logger = logging.getLogger("lkm.flows.analytics")

def _resolve_default_window() -> int:
    """解析 ``LKM_CLICKHOUSE_EXPORT_WINDOW``；非法/非正数回退 1000 并告警。

    原先写法是 import 期的 ``int(os.getenv(...))``：环境变量写成 "1e3"/"1000 " 之类
    会让本模块（乃至 flow 加载）直接抛 ValueError 起不来，而 0/负数又会一路传到
    exporter 变成 ``LIMIT 0`` 空跑或非法 SQL。
    """
    raw = os.getenv("LKM_CLICKHOUSE_EXPORT_WINDOW")
    if raw is None:
        return 1000
    try:
        value = int(raw)
    except ValueError:
        logger.warning("LKM_CLICKHOUSE_EXPORT_WINDOW=%r 非法，回退默认 1000", raw)
        return 1000
    if value <= 0:
        logger.warning("LKM_CLICKHOUSE_EXPORT_WINDOW=%d 需为正数，回退默认 1000", value)
        return 1000
    return value


_DEFAULT_WINDOW = _resolve_default_window()


def _flow_span(traceparent: str) -> Any:
    """把 flow 执行挂到触发方 trace（跨进程续链，fail-open）。"""
    # docstring 承诺 fail-open：埋点问题绝不能阻断导出。tracing 依赖初始化失败、
    # traceparent 非法导致 span 创建抛错时，降级为无 span 继续跑
    try:
        from app.core.tracing import extract_context, tracer

        ctx = extract_context({"traceparent": traceparent}) if traceparent else None
        return tracer("lkm.flows").start_as_current_span(
            "prefect.analytics_export", context=ctx
        )
    except Exception:
        logger.warning("tracing 初始化失败，降级为无 span（fail-open）", exc_info=True)
        return contextlib.nullcontext()


async def _export_failures(*, window: int) -> int:
    """业务库 event_failures → CH（复用无 Prefect 依赖的纯体层，与回落直调同源）。"""
    from app.flows.analytics_body import run_event_failures_export

    return await run_event_failures_export(window=window)


async def _export_audits(*, window: int) -> int:
    """auth 库 audit_logs → CH（复用无 Prefect 依赖的纯体层）。"""
    from app.flows.analytics_body import run_audit_logs_export

    return await run_audit_logs_export(window=window)


# Prefect task 包装：生产获得重试与运行状态；纯函数体可被编排层注入替换（测试用普通
# 函数，避免触碰 Prefect engine）。
@task(name="analytics-export-failures", retries=3, retry_delay_seconds=30)
async def export_failures_task(*, window: int) -> int:
    return await _export_failures(window=window)


@task(name="analytics-export-audits", retries=3, retry_delay_seconds=30)
async def export_audits_task(*, window: int) -> int:
    return await _export_audits(window=window)


async def orchestrate_analytics_export(
    *,
    window: int,
    export_failures: Callable[..., Awaitable[int]],
    export_audits: Callable[..., Awaitable[int]],
) -> dict[str, Any]:
    """纯编排（不依赖 Prefect）：两路各自导出至水位追平。

    生产注入 Prefect task（带重试），测试注入普通函数——两条路径共用同一控制流。

    两路**并发且互不阻塞**（gather + return_exceptions）：原先先后 await，一路抛错就
    再也不会跑另一路——某路持续失败（毒行/CH 瞬时不可用）会让 audit_logs 静默断供，
    与模块 docstring「一路失败不影响另一路」的说法相悖。两路各自开各自 realm 的会话
    与 client，故并发安全。
    """
    if window <= 0:
        # 0 → LIMIT 0 永久空跑（水位也不推进）；负数 → 非法 SQL
        raise ValueError(f"window 必须为正数，收到 {window}")
    results = await asyncio.gather(
        export_failures(window=window),
        export_audits(window=window),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    for err in errors:
        logger.error("analytics 导出一路失败: %r", err, exc_info=err)
    if errors:
        # 两路都已尝试（gather 保证互不阻塞），再抛出第一处错误让 Prefect/cron 感知失败
        raise errors[0]
    return {"event_failures": results[0], "audit_logs": results[1]}


@flow(name="analytics-clickhouse-export", retries=1, retry_delay_seconds=60)
async def analytics_export_flow(
    *,
    window: int = _DEFAULT_WINDOW,
    traceparent: str = "",
) -> dict[str, Any]:
    """ClickHouse 分析导出 flow（生产入口，task 带重试）。"""
    with _flow_span(traceparent):
        return await orchestrate_analytics_export(
            window=window,
            export_failures=export_failures_task,
            export_audits=export_audits_task,
        )


def main() -> None:
    """CLI：运维手工补导（参数经 Settings/环境，不裸传凭据）。"""
    parser = argparse.ArgumentParser(description="ClickHouse 分析导出 flow 入口")
    parser.add_argument("--window", type=int, default=_DEFAULT_WINDOW)
    parser.add_argument("--traceparent", default="", help="可选：续接父 trace")
    args = parser.parse_args()
    result = asyncio.run(_run_cli(args.window, args.traceparent))
    logger.info("analytics 导出 flow 完成: %s", result)


async def _run_cli(window: int, traceparent: str) -> dict[str, Any]:
    """CLI 包装：跑完 flow 关闭 CH 客户端。

    flow 在常驻 prefect-worker 进程内复用单例连接（不关），但 CLI 是一次性进程——
    不显式关闭会遗留 aiohttp connector（退出时报 Unclosed connector）。
    """
    from app.core import clickhouse

    try:
        return await analytics_export_flow(window=window, traceparent=traceparent)
    finally:
        await clickhouse.close()


if __name__ == "__main__":
    main()
