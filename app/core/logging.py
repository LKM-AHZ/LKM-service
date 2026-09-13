"""JSON 结构化日志基座（可观测基座 · 模块0）。

统一观测字段：request_id / latency / status / route / method。
- 请求中间件在 main.py 中写入 contextvar `request_id`，所有 logger 记录自动带上该字段。
- `setup_logging()` 在应用入口调用一次：根 logger 挂单 JSON handler 输出到 stderr。
- 结构化字段通过 `logger.info(msg, extra={"extra_fields": {...}})` 附带（见 _extra_fields）。
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# 请求级上下文：日志中间件写入，JsonFormatter 读取；未开启则默认空串。
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def set_request_id(request_id: str) -> Any:
    """为当前协程写入 request_id，返回用于恢复的 reset token。"""
    return _request_id.set(request_id)


def reset_request_id(token: Any) -> None:
    """恢复被 set_request_id 覆盖前的请求 id（中间件 finally 调用）。"""
    _request_id.reset(token)


def get_request_id() -> str:
    """读取当前协程的 request_id（无则空串）。"""
    return _request_id.get()


def current_trace_ids() -> tuple[str, str] | None:
    """读取当前 OTel span 的 (trace_id, span_id)；无埋点/无活动 span 返回 None。

    惰性 import + 全程 try/except：未装/未启用 OpenTelemetry 时静默返回 None，
    日志基座不因可观测可选件缺失而受影响（fail-open）。trace_id 为 32 位、span_id 16 位
    小写十六进制（OTel 规范），供日志与 SigNoz 链路对齐。
    """
    try:
        from opentelemetry import trace as otel_trace

        ctx = otel_trace.get_current_span().get_span_context()
    except Exception:
        return None
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON，携带 request_id 与结构化字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        trace_ids = current_trace_ids()
        if trace_ids is not None:
            payload["trace_id"], payload["span_id"] = trace_ids
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger：单 JSON handler 输出到 stderr（uvicorn 惯例）。

    幂等：若根 logger 已带 StreamHandler 则只补 formatter，不重复挂 handler。
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.formatter is None:
            handler.setFormatter(JsonFormatter())
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)
