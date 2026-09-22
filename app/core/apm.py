"""可观测基座 · Sentry APM 接入（模块0）。

空 DSN 关闭（dev/test 默认不加载，避免拖启动/发无用事件）；配置 `LKM_SENTRY_DSN` 才初始化。
初始化失败视为 fail-open：仅记为日志，不阻塞应用启动。
"""

import logging

from app.core.config import settings
from app.core.secrets import reveal

logger = logging.getLogger(__name__)

# 已成功初始化标记：sentry_sdk.init 不做去重（重复调用会另建并替换全局 Client，
# 连带重建 FastApi/Sqlalchemy 集成与 transport），故由本模块短路成真正的幂等
_initialized = False


def _traces_sample_rate() -> float:
    """采样率显式校验并夹取到 [0, 1]。

    config 的 ``sentry_traces_sample_rate`` 已带 ge=0/le=1 约束，环境变量写错
    （如 ``LKM_SENTRY_TRACES_SAMPLE_RATE=50``）在装配期即报错；这里再夹一次是给
    「绕过校验直接改 settings 属性」的场景兜底（测试 monkeypatch、进程内改配置），
    毕竟 sentry-sdk 对越界值只在内部记一条 error 然后**静默不采样**，表现为
    「初始化成功但 tracing 全关」，很难排查。
    """
    rate = settings.sentry_traces_sample_rate
    if 0.0 <= rate <= 1.0:
        return rate
    clamped = min(1.0, max(0.0, rate))
    logger.warning(
        "sentry_traces_sample_rate=%r 越界（应在 [0, 1]），已按 %r 使用", rate, clamped
    )
    return clamped


def init_sentry() -> None:
    """按 settings.sentry_dsn 初始化 Sentry；空 DSN 直返不做任何事（幂等）。"""
    global _initialized
    if _initialized:
        return
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
            traces_sample_rate=_traces_sample_rate(),
            integrations=[
                FastApiIntegration(),
                SqlalchemyIntegration(),
            ],
        )
        _initialized = True
        logger.info("Sentry APM 已初始化")
    except Exception as exc:
        # sentry_sdk 的 BadDsn 会把完整 DSN（含公钥/私钥段）回显进异常消息，而 DSN 是被
        # SecretStr 包裹的敏感值。这里只记「类型 + 脱敏后的消息」，**不带 exc_info**：
        # traceback 末行就是异常 repr，会把 DSN 原样再打一遍（实测），等于白脱敏。
        logger.error(
            "Sentry 初始化失败，降级为不加载（fail-open）: %s: %s",
            type(exc).__name__,
            str(exc).replace(dsn, "***"),
        )
