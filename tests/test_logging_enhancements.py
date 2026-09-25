"""蓝图 §6.2 的日志三项（标准库等价实现，不引入 loguru）。

- 多 sink：dev 走彩色终端、其余（含生产）走无色 JSON → stderr；
- 彩色**只**在 env=dev（生产彩色会破坏采集端解析）；
- :func:`app.core.logging.log_exceptions` = loguru ``logger.catch`` 的等价物。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from app.core import logging as lkm_logging
from app.core.config import settings


def _record(level: int = logging.ERROR, msg: str = "boom") -> logging.LogRecord:
    return logging.LogRecord("t", level, __file__, 1, msg, None, None)


class TestFormatterSelection:
    def should_use_console_formatter_only_in_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "env", "dev")
        assert isinstance(
            lkm_logging._make_formatter(), lkm_logging.DevConsoleFormatter
        )
        # test/local 也非生产，但跑的是自动化断言——彩色的 ANSI 会污染输出与快照
        for env in ("production", "prod", "staging", "test", "local"):
            monkeypatch.setattr(settings, "env", env)
            assert isinstance(lkm_logging._make_formatter(), lkm_logging.JsonFormatter)

    def should_never_emit_ansi_in_json(self) -> None:
        out = lkm_logging.JsonFormatter().format(_record())
        assert "\033[" not in out
        assert "boom" in out

    def should_color_in_dev_console(self) -> None:
        out = lkm_logging.DevConsoleFormatter().format(_record())
        assert "\033[" in out  # 有着色
        assert "boom" in out


@pytest.fixture
def _isolated_root_logger() -> Iterator[None]:
    """保存/恢复根 logger 的 handlers 与级别：setup_logging 改的是全局状态。"""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        root.handlers[:] = []
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


class TestSetupLoggingSink:
    def should_attach_json_handler_outside_dev(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_root_logger: None
    ) -> None:
        monkeypatch.setattr(settings, "env", "production")
        lkm_logging.setup_logging()
        root = logging.getLogger()
        assert root.handlers, "至少要挂一个 handler"
        assert all(
            isinstance(h.formatter, lkm_logging.JsonFormatter) for h in root.handlers
        )

    def should_attach_console_handler_in_dev(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_root_logger: None
    ) -> None:
        monkeypatch.setattr(settings, "env", "dev")
        lkm_logging.setup_logging()
        root = logging.getLogger()
        assert any(
            isinstance(h.formatter, lkm_logging.DevConsoleFormatter)
            for h in root.handlers
        )

    def should_be_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_root_logger: None
    ) -> None:
        monkeypatch.setattr(settings, "env", "production")
        lkm_logging.setup_logging()
        before = len(logging.getLogger().handlers)
        lkm_logging.setup_logging()
        assert len(logging.getLogger().handlers) == before


class TestLogExceptions:
    async def should_log_and_reraise_async(self, caplog: pytest.LogCaptureFixture) -> None:
        @lkm_logging.log_exceptions
        async def _boom() -> None:
            raise ValueError("async-ouch")

        with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
            await _boom()

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None  # 堆栈被记下
        assert errors[0].extra_fields["boundary"].endswith("_boom")  # 定位字段

    def should_log_and_reraise_sync(self, caplog: pytest.LogCaptureFixture) -> None:
        @lkm_logging.log_exceptions
        def _boom() -> None:
            raise ValueError("sync-ouch")

        with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
            _boom()

        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    async def should_swallow_when_reraise_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """带参数的用法：把失败降级为「跳过」时必须显式声明，且仍留日志。"""

        @lkm_logging.log_exceptions(reraise=False)
        async def _boom() -> str:
            raise ValueError("swallowed")

        with caplog.at_level(logging.ERROR):
            assert await _boom() is None
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    async def should_return_value_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @lkm_logging.log_exceptions
        async def _ok() -> str:
            return "fine"

        with caplog.at_level(logging.ERROR):
            assert await _ok() == "fine"
        assert caplog.records == []  # 成功路径不留任何 ERROR
