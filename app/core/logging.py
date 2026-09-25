"""JSON 结构化日志基座（可观测基座 · 模块0）。

统一观测字段：request_id / latency / status / route / method。
- 请求中间件在 main.py 中写入 contextvar `request_id`，所有 logger 记录自动带上该字段。
- `setup_logging()` 在应用入口调用一次：按 env 选 sink——**dev 走彩色终端**（本地可读），
  其余（含生产）走**无色 JSON → stderr**（彩色会破坏采集端解析，绝不能进生产）。
- 结构化字段通过 `logger.info(msg, extra={"extra_fields": {...}})` 附带（见 _extra_fields）。
- 边界函数用 :func:`log_exceptions` 装饰，等价于 loguru 的 ``logger.catch``（蓝图 §6.2）。

**不引入 loguru**：本仓的日志契约是「标准库 + 统一 JSON」，再引一套日志体系会破坏既有
统一输出（蓝图 §6.2 明示）。
"""

import functools
import inspect
import json
import logging
import sys
from collections.abc import Awaitable, Callable
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


# 核心 schema 字段：extra_fields 不得覆盖（撞名会让调用方伪造/抹掉 ts、level、
# request_id 等关联字段，属日志伪造面）
_CORE_FIELDS: frozenset[str] = frozenset(
    {"ts", "level", "logger", "msg", "request_id", "trace_id", "span_id", "exc_info"}
)


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
            payload.update(
                {
                    key: value
                    for key, value in extra_fields.items()
                    if key not in _CORE_FIELDS
                }
            )
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # default=str 兜底：datetime/ORM 对象/set 等不可序列化值原先会让 json.dumps 抛错，
        # 被 Handler.emit → handleError 吞掉后**整条日志**丢失（只剩一个 traceback）
        return json.dumps(payload, ensure_ascii=False, default=str)


# —— dev 彩色终端（仅本地）——
# 标准库 ANSI，不引 Rich/colorlog（零新依赖；蓝图 §6.2 要求彩色**只**在 dev 生效）。
_RESET = "\033[0m"
_DIM = "\033[2m"
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",  # cyan
    "INFO": "\033[32m",  # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "CRITICAL": "\033[1;31m",  # bold red
}


class DevConsoleFormatter(logging.Formatter):
    """本地开发用的可读单行格式（级别上色 + 时间 + logger + 结构化字段）。

    **只在 env=dev 生效**（见 :func:`_make_formatter`）。生产必须走 :class:`JsonFormatter`
    的无色 JSON——ANSI 转义会污染采集端解析。
    """

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S.%f")[:-3]
        line = (
            f"{_DIM}{ts}{_RESET} {color}{record.levelname:<8}{_RESET} "
            f"{_DIM}{record.name}{_RESET} {record.getMessage()}"
        )
        request_id = get_request_id()
        if request_id:
            line = f"{line} {_DIM}req={request_id}{_RESET}"
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            shown = {k: v for k, v in extra_fields.items() if k not in _CORE_FIELDS}
            if shown:
                line = (
                    f"{line} {_DIM}"
                    f"{json.dumps(shown, ensure_ascii=False, default=str)}{_RESET}"
                )
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _use_dev_console() -> bool:
    """是否用开发彩色终端格式：**仅** ``env=dev``。

    不用 ``is_production``——localhost/test 也非生产，但它们跑的是自动化断言，彩色会污染
    输出与快照（蓝图 §6.2 的措辞也是"仅 dev"）。settings 惰性 import，避免 config ↔ logging
    的模块级环。
    """
    try:
        from app.core.config import settings

        return settings.env.strip().lower() == "dev"
    except Exception:
        return False


def _make_formatter() -> logging.Formatter:
    return DevConsoleFormatter() if _use_dev_console() else JsonFormatter()


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger：dev 走彩色终端、其余走 JSON → stderr（uvicorn 惯例）。

    幂等（可重复调用）：
    - 已有的 stream handler 一律换成当前 env 对应的 formatter——早先配的纯文本 handler
      （basicConfig/--log-config/第三方库）若保持原样会一直生效，统一格式被静默关闭；
    - 只有当根 logger 上**确有 stderr handler** 时才不新挂：原判据是「存在任意
      StreamHandler」，会被 FileHandler（StreamHandler 子类）满足而漏挂 stderr。
    """
    root = logging.getLogger()
    formatter = _make_formatter()
    stderr_handler: logging.StreamHandler | None = None
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setFormatter(formatter)
            if getattr(handler, "stream", None) is sys.stderr:
                stderr_handler = handler
    if stderr_handler is None:
        new_handler = logging.StreamHandler(sys.stderr)
        new_handler.setFormatter(formatter)
        root.addHandler(new_handler)
    root.setLevel(level)


def log_exceptions[**P, R](
    func: Callable[P, R] | Callable[P, Awaitable[R]] | None = None,
    *,
    logger_: logging.Logger | None = None,
    reraise: bool = True,
    message: str | None = None,
) -> Any:
    """边界函数的异常兜底：自动记 ``exc_info`` + 定位字段（loguru ``logger.catch`` 的等价物）。

    蓝图 §6.2 要求用标准库实现该能力。适用于「异常必须有日志，否则线上只剩一条无上下文的
    堆栈」的**边界**：consumer handler、APScheduler 任务、service 入口。

    - 同步/异步函数都支持；``reraise=True``（默认）不改变控制流，只补日志。
    - ``reraise=False`` 时吞掉异常并返回 None——只在你确实要把失败降级为「跳过」时用。
    - **别与手写的 ``except: logger.exception(...)`` 叠用**：内层已手工记过的地方再加装饰器
      会让同一次异常记两遍、污染日志与告警计数。
    """

    def _decorate(
        fn: Callable[P, R] | Callable[P, Awaitable[R]],
    ) -> Callable[P, Any]:
        lg = logger_ or logging.getLogger(fn.__module__)
        label = message or f"{fn.__qualname__} 抛出异常"

        def _record(exc: BaseException) -> None:
            lg.error(
                label,
                exc_info=exc,
                extra={"extra_fields": {"boundary": fn.__qualname__}},
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    _record(exc)
                    if reraise:
                        raise
                    return None

            return _async_wrapper

        @functools.wraps(fn)
        def _sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                _record(exc)
                if reraise:
                    raise
                return None

        return _sync_wrapper

    if func is None:
        return _decorate
    return _decorate(func)
