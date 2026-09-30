"""可观测基座（模块0）：JsonFormatter 与请求中间件 request_id 注入。"""

import json
import logging

from core import logging as lkm_logging
from core.logging import JsonFormatter


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="lkm.http",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_formatter_emits_valid_json() -> None:
    """JsonFormatter 输出合法 JSON，含 ts/level/logger/msg 字段。"""
    payload = json.loads(JsonFormatter().format(_make_record("hello")))
    assert payload["logger"] == "lkm.http"
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello"
    assert "ts" in payload


def test_json_formatter_carries_request_id() -> None:
    """设置 request_id contextvar 后，格式化自动带上该字段。"""
    token = lkm_logging.set_request_id("req-123")
    try:
        payload = json.loads(JsonFormatter().format(_make_record("x")))
    finally:
        lkm_logging.reset_request_id(token)
    assert payload["request_id"] == "req-123"


def test_json_formatter_includes_extra_fields() -> None:
    """extra={"extra_fields": {...}} 的结构化字段被并入 JSON，避免浮层嵌套。"""
    record = _make_record("y")
    record.extra_fields = {"status": 200, "latency_ms": 1.5}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["status"] == 200
    assert payload["latency_ms"] == 1.5


def test_request_id_reset_restores_default() -> None:
    """reset 后 request_id 恢复为空串。"""
    token = lkm_logging.set_request_id("req-1")
    lkm_logging.reset_request_id(token)
    assert lkm_logging.get_request_id() == ""


async def test_http_middleware_logs_and_sets_header(client, caplog) -> None:
    """请求中间件记录结构化访问日志并回写 X-Request-ID 响应头。"""
    import app.main as main_mod

    caplog.set_level(logging.INFO, logger="lkm.http")
    resp = await client.get("/")
    assert resp.headers.get("X-Request-ID")
    assert resp.status_code == 200
    # 中间件通过闭包引用 request_logger（即 main_mod.request_logger）
    found = any(
        r.name == "lkm.http" and "http.request" in r.getMessage()
        for r in caplog.records
    )
    assert found, "中间件应至少产生一条 http.request 访问日志"
    assert main_mod.request_logger.name == "lkm.http"
