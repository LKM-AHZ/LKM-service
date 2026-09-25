"""Prefect flow：运营日报（蓝图 §5.5/§6.4 列为「运营日报」）。

与 ``analytics`` / ``user_dim`` 同构：纯体层（``ops_daily_body``）→ Prefect task（带重试）→
注入式纯编排。**最小版**：只聚合**已经落库**的数据（ClickHouse 的永久失败事件与行为审计 +
业务库内容日新增），产出走运行日志（Prefect 的运行日志本身被持久化，可在 UI 回看）。

刻意不新建表、不改 schema；也不碰在线口径（活跃用户/发帖趋势已有 admin 端点实时算）。
后续若要扩充口径或改成落表供端点查询，属产品决策，届时另起变更。

CLI（手工补跑某一天）::

    python -m app.flows.ops_daily --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from prefect import flow, task

logger = logging.getLogger("lkm.flows.ops_daily")

# 日报默认窗口：近 1 天（每天跑一次，覆盖上一日）
_DEFAULT_DAYS = 1


def _flow_span(traceparent: str) -> Any:
    """把 flow 执行挂到触发方 trace（跨进程续链，fail-open）。"""
    try:
        from app.core.tracing import extract_context, tracer

        ctx = extract_context({"traceparent": traceparent}) if traceparent else None
        return tracer("lkm.flows").start_as_current_span(
            "prefect.ops_daily", context=ctx
        )
    except Exception:
        logger.warning("tracing 初始化失败，降级为无 span（fail-open）", exc_info=True)
        return contextlib.nullcontext()


async def _collect(*, days: int) -> dict[str, Any]:
    """复用无 Prefect 依赖的纯体层（与 CLI/回落直调同源）。"""
    from app.flows.ops_daily_body import collect_daily_report

    return await collect_daily_report(days=days)


@task(name="ops-daily-collect", retries=3, retry_delay_seconds=30)
async def collect_daily_task(*, days: int) -> dict[str, Any]:
    return await _collect(days=days)


async def orchestrate_ops_daily(
    *,
    days: int,
    collect: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """纯编排（不依赖 Prefect）：校验窗口后收集日报。

    生产注入 Prefect task（带重试），测试注入普通函数——两条路径共用同一控制流。
    """
    if days <= 0:
        raise ValueError(f"days 必须为正数，收到 {days}")
    return await collect(days=days)


@flow(name="ops-daily-report", retries=1, retry_delay_seconds=60)
async def ops_daily_flow(
    *,
    days: int = _DEFAULT_DAYS,
    traceparent: str = "",
) -> dict[str, Any]:
    """运营日报 flow（生产入口，task 带重试）。"""
    with _flow_span(traceparent):
        return await orchestrate_ops_daily(days=days, collect=collect_daily_task)


def main() -> None:
    """CLI：运维手工补跑（参数经 Settings/环境，不裸传凭据）。"""
    parser = argparse.ArgumentParser(description="运营日报 flow 入口")
    parser.add_argument("--days", type=int, default=_DEFAULT_DAYS)
    parser.add_argument("--traceparent", default="", help="可选：续接父 trace")
    args = parser.parse_args()
    result = asyncio.run(_run_cli(args.days, args.traceparent))
    logger.info("运营日报 flow 完成: %s", result)


async def _run_cli(days: int, traceparent: str) -> dict[str, Any]:
    """CLI 包装：跑完关闭 CH 客户端（一次性进程，不关会遗留 aiohttp connector）。"""
    from app.core import clickhouse

    try:
        return await ops_daily_flow(days=days, traceparent=traceparent)
    finally:
        await clickhouse.close()


if __name__ == "__main__":
    main()
