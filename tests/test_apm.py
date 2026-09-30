"""可观测基座（模块0）：Sentry 接入的 DSN 空开关。"""

from typing import Any, cast

from core.config import settings


async def test_init_sentry_noop_when_dsn_empty(monkeypatch) -> None:
    """空 DSN → init_sentry 不加载 sentry_sdk（fail-open 开关）。"""
    import core.apm as apm

    monkeypatch.setattr(settings, "sentry_dsn", "")

    def _fail_if_init(**kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("sentry_sdk.init 不应被调用（DSN 空）")

    import sys

    fake_module: Any = cast(Any, type(sys)("sentry_sdk"))
    fake_module.init = _fail_if_init
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_module)

    apm.init_sentry()  # 不应抛错，也不应调用 sentry_sdk.init
