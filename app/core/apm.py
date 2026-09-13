"""可观测基座 · Sentry APM 接入（模块0）。

空 DSN 关闭（dev/test 默认不加载，避免拖启动/发无用事件）；配置 `LKM_SENTRY_DSN` 才初始化。
初始化失败视为 fail-open：仅记为日志，不阻塞应用启动。
"""

import logging

from app.core.config import settings
from app.core.secrets import reveal

logger = logging.getLogger(__name__)


def init_sentry() -> None:
    """按 settings.sentry_dsn 初始化 Sentry；空 DSN 直返不做任何事（幂等）。"""
    dsn = reveal(settings.sentry_dsn)
    if not dsn:
        logger.info("Sentry DSN 未配置，跳过初始化（可观测可选）")
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=settings.env or "unknown",
            traces_sample_rate=settings.sentry_traces_sample_rate,
            integrations=[
                FastApiIntegration(),
                SqlalchemyIntegration(),
            ],
        )
        logger.info("Sentry APM 已初始化")
    except Exception:
        logger.exception("Sentry 初始化失败，降级为不加载（fail-open）")
