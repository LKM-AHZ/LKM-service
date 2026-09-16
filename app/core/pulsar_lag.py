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
import time
from contextlib import suppress
from typing import Any, cast

import httpx

from app.core.config import settings
from app.core.messaging import SUBSCRIPTIONS
from app.core.metrics import pulsar_subscription_backlog
from app.core.secrets import reveal

logger = logging.getLogger("lkm.pulsar_lag")

_task: asyncio.Task[None] | None = None

# readiness 探活的「up」结果缓存：(单调时刻, 结果)。只缓存成功——error 不入缓存，
# 恢复立即可见，且不会把陈旧的健康读成就绪（见 app.modules.health.router._probe_pulsar）。
_probe_cache: tuple[float, tuple[str, str | None]] | None = None

# 可注入的出站 client 工厂（测试离线驱动用）：默认 None → 按配置超时新建。
# 与 health router 的 _auth_liveness_factory / auth.user_http._client_factory 同款范式。
_probe_client_factory: Any = None


def _stats_path(topic: str) -> str:
    """persistent://lkm/biz/points.apply → /admin/v2/persistent/lkm/biz/points.apply/stats"""
    rest = topic.removeprefix("persistent://")
    return f"/admin/v2/persistent/{rest}/stats"


async def _collect_once(client: httpx.AsyncClient) -> None:
    headers = _admin_headers()
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


def _admin_headers() -> dict[str, str]:
    """Admin REST 鉴权头（standalone 无 token 时为默认超集）。"""
    token = reveal(settings.pulsar_admin_token)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _build_probe_client(timeout: float) -> httpx.AsyncClient:
    if _probe_client_factory is not None:
        return cast(httpx.AsyncClient, _probe_client_factory())
    return httpx.AsyncClient(timeout=timeout)


async def probe_health(timeout_s: float | None = None) -> tuple[str, str | None]:
    """探 Pulsar broker 健康（Admin REST ``/admin/v2/brokers/health``）。

    返回 ``(status, detail)``，status ∈ ``up | disabled | error``：

    - ``disabled``：未启用消息总线或未配 ``pulsar_admin_url`` → **不计入 readiness 硬依赖**
      （单机/无总线部署语义明确），调用方不应把其当故障。
    - ``up``/``error``：真探 Admin REST（纯文本 ``ok`` 为健康）；短超时 fail-fast，
      探活异常一律转 ``error`` 返回、不向上抛（就绪探针不因底层抖动 500）。

    复用 lag 上报的同一 Admin REST 通道与鉴权头，不新起长连（一次性短连，见 M6.2 设计）。
    """
    if not settings.message_bus_enabled or not settings.pulsar_admin_url:
        return "disabled", "pulsar 未配置"

    global _probe_cache
    now = time.monotonic()
    if _probe_cache is not None:
        cached_at, cached = _probe_cache
        if now - cached_at < settings.pulsar_probe_cache_s:
            return cached

    result = await _probe_health_once(
        timeout_s if timeout_s is not None else settings.pulsar_probe_timeout_s
    )
    _probe_cache = (now, result) if result[0] == "up" else None
    return result


async def _probe_health_once(timeout_s: float) -> tuple[str, str | None]:
    url = f"{settings.pulsar_admin_url.rstrip('/')}/admin/v2/brokers/health"
    try:
        async with _build_probe_client(timeout_s) as client:
            resp = await client.get(url, headers=_admin_headers())
    except Exception as exc:  # 不可达/超时：fail-fast 转 error，不向上抛
        return "error", f"pulsar 不可达: {exc}"
    if resp.status_code != 200:
        return "error", f"broker health http {resp.status_code}"
    if resp.text.strip().lower() != "ok":
        return "error", f"broker 未就绪: {resp.text.strip()[:80]}"
    return "up", None


async def stop_lag_reporter() -> None:
    """取消 lag 上报任务（幂等/可重复调用）。"""
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
