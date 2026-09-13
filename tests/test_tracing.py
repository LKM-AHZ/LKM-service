"""M5 7.2.2 OTel 埋点验收：开关语义、失败降级、span 产出与日志 trace_id 关联。

用**每次新建的 FastAPI 应用**测 instrument（Starlette 中间件栈一旦构建不能再 add，
全局 app 反复 instrument 会炸）；exporter 经 ``tracing._exporter_factory`` seam 注入
内存实现，不依赖真实 collector。
"""

from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.core import logging as lkm_logging
from app.core import tracing
from app.core.config import settings


def _fresh_app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def _root() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _cleanup_tracing():
    yield
    tracing.shutdown_tracing()


def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_enabled", False)
    tracing.setup_tracing(_fresh_app())
    assert tracing.is_enabled() is False
    tracing.shutdown_tracing()  # 幂等，不抛


async def test_span_exported_and_log_correlated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_sample_ratio", 1.0)
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(tracing, "_exporter_factory", lambda: exporter)

    app = _fresh_app()
    tracing.setup_tracing(app)
    assert tracing.is_enabled() is True

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200

    provider = tracing._tracer_provider
    assert provider is not None
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert spans, "GET / 应产出一条 span"
    assert any("GET /" in (s.name or "") or "/" in (s.name or "") for s in spans)

    # 日志关联：活动 span 内 JsonFormatter 输出 trace_id/span_id，且与 span 一致
    with tracing.tracer("test").start_as_current_span("probe") as span:
        ids = lkm_logging.current_trace_ids()
        sc = span.get_span_context()
        assert ids == (format(sc.trace_id, "032x"), format(sc.span_id, "016x"))
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hi", None, None)
        payload = json.loads(lkm_logging.JsonFormatter().format(record))
    assert payload["trace_id"] == ids[0]
    assert payload["span_id"] == ids[1]

    tracing.shutdown_tracing()
    assert tracing.is_enabled() is False


def test_unreachable_exporter_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """collector 不可达：setup 不抛、shutdown 限时 flush 快速返回（fail-open）。"""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_sample_ratio", 1.0)
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "http://127.0.0.1:9/v1/traces")
    monkeypatch.setattr(settings, "otel_exporter_timeout_s", 1.0)

    tracing.setup_tracing(_fresh_app())
    assert tracing.is_enabled() is True
    with tracing.tracer("test").start_as_current_span("will-fail-export"):
        pass

    start = time.perf_counter()
    tracing.shutdown_tracing()
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"shutdown 应快速返回，实测 {elapsed:.2f}s"
    assert tracing.is_enabled() is False
