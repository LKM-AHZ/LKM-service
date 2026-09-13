"""Pulsar 订阅 lag 上报（M4 可观测）。

Pulsar Python 客户端不直接暴露订阅积压，故经 **Pulsar Admin REST** 周期拉取每个 topic 的
``/admin/v2/persistent/{tenant}/{namespace}/{topic}/stats``，取
``subscriptions.{name}.msgBacklog`` 写入 ``pulsar_subscription_backlog`` gauge。

只在 **API 进程**（暴露 /metrics 的进程）启动；worker 进程不暴露指标端点，故不上报。
未配置消息总线或未配 ``pulsar_admin_url`` 时整体 no-op（fail-open），不影响应用启动。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, cast

import httpx

from app.core.config import settings
from app.core.messaging import SUBSCRIPTIONS
from app.core.metrics import pulsar_subscription_backlog
from app.core.secrets import reveal

logger = logging.getLogger("lkm.pulsar_lag")

_task: asyncio.Task[None] | None = None


def _stats_path(topic: str) -> str:
    """persistent://lkm/biz/points.apply → /admin/v2/persistent/lkm/biz/points.apply/stats"""
    rest = topic.removeprefix("persistent://")
    return f"/admin/v2/persistent/{rest}/stats"


async def _collect_once(client: httpx.AsyncClient) -> None:
    headers: dict[str, str] = {}
    if reveal(settings.pulsar_admin_token):
        headers["Authorization"] = f"Bearer {reveal(settings.pulsar_admin_token)}"
    # 同一 topic 可有多个订阅（points 三订阅扇出）：按 topic 去重，一次 stats 覆盖该 topic 全部订阅。
    subs_by_topic: dict[str, list[str]] = {}
    for sub in SUBSCRIPTIONS.values():
        subs_by_topic.setdefault(sub.topic, []).append(sub.name)
    for topic, names in subs_by_topic.items():
        try:
            resp = await client.get(_stats_path(topic), headers=headers)
            resp.raise_for_status()
            body = cast(dict[str, Any], resp.json())
            subscriptions = cast(dict[str, Any], body.get("subscriptions") or {})
            for name in names:
                entry = cast(dict[str, Any], subscriptions.get(name) or {})
                backlog = int(entry.get("msgBacklog", 0))
                pulsar_subscription_backlog.labels(subscription=name, topic=topic).set(
                    backlog
                )
        except Exception:
            # 单个 topic 拉取失败不中断其它 topic，也不影响主流程（保留上次 gauge 值）。
            logger.warning("lag 拉取失败 topic=%s", topic, exc_info=True)


async def _run() -> None:
    async with httpx.AsyncClient(
        base_url=settings.pulsar_admin_url, timeout=10.0
    ) as client:
        while True:
            await _collect_once(client)
            await asyncio.sleep(settings.pulsar_lag_interval_s)


def start_lag_reporter() -> None:
    """启动 lag 上报后台任务（幂等；未配置则 no-op）。"""
    global _task
    if not settings.message_bus_enabled or not settings.pulsar_admin_url:
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_run())
        logger.info(
            "Pulsar lag 上报已启动 interval=%ss", settings.pulsar_lag_interval_s
        )


async def stop_lag_reporter() -> None:
    """取消 lag 上报任务（幂等/可重复调用）。"""
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
