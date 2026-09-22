"""可观测基座 · OpenTelemetry 链路追踪（M5 7.2.2）。

默认关闭（``settings.otel_enabled=false``）：不建 provider、不 instrument，零依赖零副作用。
开启后把 FastAPI / SQLAlchemy / httpx 的 span 经 OTLP/HTTP 导出到 collector（本地
``deploy/otel`` 或外部 SigNoz），并把 trace_id/span_id 串进 JSON 日志（``core.logging``）。

设计要点（对齐 ``core.apm.init_sentry`` 的 fail-open 范式）：
- **显式传 provider**（不调 ``trace.set_tracer_provider``）：全局 provider 只能设一次，
  测试里反复 setup/shutdown 会踩"覆盖被忽略"；显式传入使每个 setup 自足可回退。
- span/context 存于 OTel contextvar，与全局 provider 无关 → 日志关联照常工作。
- 导出用 ``BatchSpanProcessor``：请求热路径不阻塞；``shutdown`` 限时 flush。
- 任何异常只记日志，绝不阻塞启动/请求。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any
from weakref import WeakSet

from app.core.config import settings

logger = logging.getLogger(__name__)

# 不埋点的健康/指标端点：collector 抓取与编排探活不产生 span 噪声
_EXCLUDED_URLS = "/metrics,/api/v1/health,/liveness,/readiness"

# 测试 seam：注入内存 exporter（如 InMemorySpanExporter）替真实 OTLP 导出
_exporter_factory: Callable[[], Any] | None = None

_tracer_provider: Any = None
# 已挂 SQLAlchemy 埋点的引擎（弱引用）：用 WeakSet 而非 id(engine)——既不阻止引擎回收
# （测试每用例新建引擎不会堆积），也不会因对象地址被回收后复用而误判「已挂过」
_sqlalchemy_engines: WeakSet[Any] = WeakSet()


def is_enabled() -> bool:
    """是否已有活动的 tracer provider（供热路径短路判断）。"""
    return _tracer_provider is not None


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for pair in (raw or "").split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def _default_exporter() -> Any:
    """构造 OTLP/HTTP span exporter；endpoint 为空时用 OTLP 缺省解析规则。"""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    kwargs: dict[str, Any] = {
        "timeout": settings.otel_exporter_timeout_s,
    }
    if settings.otel_exporter_otlp_endpoint:
        kwargs["endpoint"] = settings.otel_exporter_otlp_endpoint
    headers = _parse_headers(settings.otel_exporter_otlp_headers)
    if headers:
        kwargs["headers"] = headers
    return OTLPSpanExporter(**kwargs)


def _service_name(service_suffix: str) -> str:
    return settings.otel_service_name or f"{settings.app_name}{service_suffix}"


def setup_tracing(app: Any = None, *, service_suffix: str = "") -> None:
    """按配置初始化 OTel；未启用或失败则静默跳过（幂等、fail-open）。

    *app* 为 FastAPI 实例时额外挂 FastAPI 埋点；worker/scheduler 等**非 ASGI 进程**
    传 ``None``——它们只需 provider + httpx，消费/调度 span 经 :func:`tracer` 产出。

    **调用时机**：ASGI 进程必须在**装配期**（返回 app 前）调用。FastAPI 的中间件栈在
    首个 ASGI 请求（含 lifespan）时由 Starlette 定型，在 lifespan 内再 ``add_middleware``
    不会进入栈——表现为 HTTP server span **完全采集不到**，而 SQLAlchemy/httpx 埋点因
    不依赖中间件栈照常生效（易误判为「埋点已工作」）。
    """
    global _tracer_provider
    if _tracer_provider is not None:
        return
    if not settings.otel_enabled:
        logger.info("OpenTelemetry 未启用，跳过埋点（可观测可选）")
        return
    provider: Any = None
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )

        resource = Resource.create(
            {
                "service.name": _service_name(service_suffix),
                "deployment.environment": settings.env or "unknown",
            }
        )
        provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
        )
        provider.add_span_processor(BatchSpanProcessor(_exporter_factory_or_default()))

        if app is not None:
            FastAPIInstrumentor.instrument_app(
                app, tracer_provider=provider, excluded_urls=_EXCLUDED_URLS
            )
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        _tracer_provider = provider
        logger.info("OpenTelemetry 已初始化 service=%s", _service_name(service_suffix))
    except Exception:
        # 半初始化回滚：provider 可能已建（BatchSpanProcessor 导出线程 + OTLP session）、
        # instrumentor 可能已挂。不回收则线程/会话泄漏到进程结束，且 is_enabled() 为 False
        # 而埋点仍在，下次成功 setup 会在残留埋点上再建一个 provider（状态不一致）。
        if provider is not None:
            with contextlib.suppress(Exception):
                provider.shutdown()
        with contextlib.suppress(Exception):
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            FastAPIInstrumentor().uninstrument()
            HTTPXClientInstrumentor().uninstrument()
        _tracer_provider = None
        logger.exception("OpenTelemetry 初始化失败，降级为不埋点（fail-open）")


def _exporter_factory_or_default() -> Any:
    factory = _exporter_factory or _default_exporter
    return factory()


def instrument_sqlalchemy(engine: Any) -> None:
    """给已建好的异步引擎挂 SQLAlchemy span；未启用/已挂/失败均安全跳过。"""
    if _tracer_provider is None or engine is None:
        return
    if engine in _sqlalchemy_engines:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(
            engine=engine.sync_engine, tracer_provider=_tracer_provider
        )
        _sqlalchemy_engines.add(engine)
        logger.info("OpenTelemetry SQLAlchemy 埋点已挂载")
    except Exception:
        logger.exception("OpenTelemetry SQLAlchemy 埋点失败，跳过（fail-open）")


def tracer(name: str = "lkm") -> Any:
    """返回 tracer：有 provider 用其命名 tracer，否则用全局 no-op tracer。"""
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(name)
    from opentelemetry import trace as otel_trace

    return otel_trace.get_tracer(name)


def inject_context(carrier: dict[str, str]) -> None:
    """把当前 trace context（traceparent 等）注入消息属性，供跨进程消费端续链。"""
    if _tracer_provider is None:
        return
    with contextlib.suppress(Exception):
        from opentelemetry.propagate import inject

        inject(carrier)


def extract_context(carrier: dict[str, str]) -> Any:
    """从消息属性还原父 trace context；未启用以 None 返回（起新根 span）。"""
    if _tracer_provider is None:
        return None
    try:
        from opentelemetry.propagate import extract

        return extract(carrier)
    except Exception:
        return None


@contextlib.contextmanager
def consume_span(
    properties: dict[str, str], topic: str, subscription: str
) -> Iterator[Any]:
    """消费一条消息的 span：从 properties 续父链，标注 topic/subscription。"""
    ctx = extract_context(properties)
    with tracer("lkm.messaging").start_as_current_span(
        "pulsar.consume", context=ctx
    ) as span:
        with contextlib.suppress(Exception):
            span.set_attribute("messaging.system", "pulsar")
            span.set_attribute("messaging.destination.name", topic)
            span.set_attribute("messaging.pulsar.subscription", subscription)
        yield span


def shutdown_tracing() -> None:
    """限时 flush 并卸载 instrumentor；幂等，异常仅记日志。"""
    global _tracer_provider
    provider = _tracer_provider
    _tracer_provider = None
    _sqlalchemy_engines.clear()
    # 卸载**不依赖 provider**：半初始化失败（已挂 instrumentor 却把 provider 置空）或上一次
    # 卸载静默失败时，在此早退就再也没有机会卸载——进程里残留埋点，下次 setup 会叠加一层
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        FastAPIInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()
        SQLAlchemyInstrumentor().uninstrument()
    if provider is None:
        return
    with contextlib.suppress(Exception):
        provider.shutdown()
