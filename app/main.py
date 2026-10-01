import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response
from strawberry.fastapi import BaseContext

from app.api.graphql import (
    GRAPHQL_DEFAULT_VERSION,
    GRAPHQL_VERSIONS,
    GuardedGraphQLRouter,
    build_schema,
)
from app.api.router import api_router
from app.modules import registry
from app.ws.manager import manager
from core import clickhouse, messaging, user_cache_events
from core import logging as logger
from core import redis as redis_client
from core.apm import init_sentry
from core.config import settings
from core.contracts import CurrentUser
from core.db.init_db import init_db
from core.db.session import (
    AsyncSession,
    dispose_engine,
    get_async_engine,
)
from core.db.session import (
    get_read_session as get_graphql_session,  # GraphQL 仅 Query(纯读)，避免空提交
)
from core.err import BizError, map_err, resp_json
from core.metrics import setup_metrics
from core.metrics_relay import start_reporter as start_metrics_relay_reporter
from core.metrics_relay import stop_reporter as stop_metrics_relay_reporter
from core.middleware import GraphQLHTTPMiddleware, install_security_middleware
from core.ports.authz import get_optional_user
from core.ports.verify_keys import (
    cleanup_expired_challenges,
    start_verify_key_refresh,
    stop_verify_key_refresh,
)
from core.pulsar_lag import start_lag_reporter, stop_lag_reporter
from core.scheduler_state import start_reporter as start_scheduler_reporter
from core.scheduler_state import stop_reporter as stop_scheduler_reporter
from core.tracing import (
    instrument_sqlalchemy,
    setup_tracing,
    shutdown_tracing,
)


@dataclass
class GraphQLContext(BaseContext):
    """GraphQL 请求上下文：持有当前请求的数据库会话（只读查询）。

    会话由 GraphQLRouter 的 context_getter 经 FastAPI 依赖注入，与 REST 端点共用
    同一会话依赖，便于测试 override。讨论帖查询已统一由 content 模块承载。
    """

    db: AsyncSession
    user_id: uuid.UUID | None = None


request_logger = logging.getLogger("lkm.http")


async def _shutdown_step(name: str, step: Callable[[], Awaitable[object]]) -> None:
    """收尾单步兜底：任一步骤失败只记日志，不让它跳过其余资源的释放。"""
    try:
        await step()
    except Exception:
        request_logger.exception("shutdown step failed name=%s", name)


# 后台 schema 初始化的重试间隔范围（秒）。
_INIT_DB_RETRY_MIN_S = 1.0
_INIT_DB_RETRY_MAX_S = 30.0


async def _init_db_with_retry() -> None:
    """后台把业务库 schema 初始化到最新，失败按指数退避重试（启动不阻塞，§2 第 1 条）。

    蓝图要求「进程启动不等待任何下游就绪」：DB 暂时不可用时进程照常起，liveness/readiness
    照常应答（readiness 报未就绪 → 不入流），DB 恢复后自行续上、**无需重启进程**。成功即
    返回；未成功前 ``is_db_initialized()`` 恒为 False，readiness 据此保持未就绪。
    """
    delay = _INIT_DB_RETRY_MIN_S
    while True:
        try:
            await init_db()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            request_logger.exception("schema init failed; retry in %.0fs", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, _INIT_DB_RETRY_MAX_S)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    # 可观测基座：结构化日志 + Sentry APM（均幂等；DSN 空则不加载）
    logger.setup_logging()
    init_sentry()
    init_db_task = asyncio.create_task(_init_db_with_retry())
    instrument_sqlalchemy(get_async_engine())
    await user_cache_events.start()

    await start_verify_key_refresh()

    cleanup_task = asyncio.create_task(cleanup_expired_challenges())

    # 可观测（M4）：Pulsar 订阅 lag 周期上报（未配置则 no-op）
    start_lag_reporter()
    start_scheduler_reporter()
    # 聚合 worker、scheduler 和 auth 的 Redis 指标快照。
    start_metrics_relay_reporter()

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    except Exception:
        # 清理异常只记日志，继续释放其他资源。
        request_logger.exception("background cleanup task failed during shutdown")

    # 关闭前取消 schema 初始化任务。
    init_db_task.cancel()
    try:
        await init_db_task
    except asyncio.CancelledError:
        pass
    except Exception:
        request_logger.exception("schema init task failed during shutdown")

    await _shutdown_step("ws_manager", manager.close)
    await _shutdown_step("user_cache_events", user_cache_events.stop)
    # 收尾验签公钥刷新 task（唯一在途的出站请求在此被取消）
    await _shutdown_step("verify_key_refresh", stop_verify_key_refresh)
    await _shutdown_step("pulsar_lag", stop_lag_reporter)
    await _shutdown_step("scheduler_state", stop_scheduler_reporter)
    await _shutdown_step("metrics_relay", stop_metrics_relay_reporter)
    await _shutdown_step("messaging", messaging.shutdown)
    # 收尾 ClickHouse 客户端（若 admin 查询曾建连；未启用则 no-op）
    await _shutdown_step("clickhouse", clickhouse.close)
    await _shutdown_step("redis", redis_client.close_redis)
    # 收尾链路追踪（限时 flush），先于引擎释放；同步函数，同样兜底
    try:
        shutdown_tracing()
    except Exception:
        request_logger.exception("shutdown step failed name=tracing")
    await _shutdown_step("engine", dispose_engine)


def create_app() -> FastAPI:
    # 加载业务模块与错误码。
    registry.load_all()

    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    application.add_middleware(GraphQLHTTPMiddleware)

    @application.middleware("http")
    async def _log_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """结构化访问日志：记录 method/route/status/latency。

        request_id 的生成/净化/回写响应头已上移到 ``core.middleware.RequestIdMiddleware``
        （两进程共用，auth 进程原本完全没有 → 其响应无头也无日志关联）。这里只读同一次
        请求已注入 ContextVar 的值。
        """
        start = time.perf_counter()
        try:
            response = await call_next(request)
            latency_ms = (time.perf_counter() - start) * 1000
            request_logger.info(
                "http.request",
                extra={
                    "extra_fields": {
                        "method": request.method,
                        "route": request.url.path,
                        "status": response.status_code,
                        "latency_ms": round(latency_ms, 3),
                    }
                },
            )
            return response
        except Exception:
            # 业务异常会穿过本中间件交给外层 ServerErrorMiddleware 的 Exception handler；
            # 若只记成功路径，恰恰最需要关联信息的 500 请求没有任何访问日志（重抛不放行）
            request_logger.exception(
                "http.request",
                extra={
                    "extra_fields": {
                        "method": request.method,
                        "route": request.url.path,
                        "status": 500,
                        "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                    }
                },
            )
            raise

    install_security_middleware(application)

    setup_tracing(application)

    application.include_router(api_router, prefix=settings.api_prefix)
    application.add_exception_handler(BizError, _on_err)
    application.add_exception_handler(RequestValidationError, _on_err)
    application.add_exception_handler(Exception, _on_err)

    async def _graphql_context(
        db: AsyncSession = Depends(get_graphql_session),
        cur: CurrentUser | None = Depends(get_optional_user),
    ) -> GraphQLContext:
        # 会话生命周期由 FastAPI 的 Depends 管理，解析器只读不关闭；
        # cur 可选（带 Bearer 则解析出 user_id，供关注流/时间线等按登录态个性化）。
        return GraphQLContext(db=db, user_id=cur.id if cur is not None else None)

    # §2 多端点版本化：每个版本各挂一个**独立 schema** 的端点（`{graphql_path}/vN`），
    # 旧端点永久保留服务存量客户端；网关按 X-API-Version 分流（deploy/apisix/apisix.yaml）。
    for _version in GRAPHQL_VERSIONS:
        application.include_router(
            GuardedGraphQLRouter(
                build_schema(_version),  # §7：registry 按版本聚合模块 GraphQL Query
                path=f"{settings.graphql_path}/{_version}",
                context_getter=_graphql_context,
            )
        )
    # 无版本路径 = 默认版本别名（与 REST 的「不带版本」对称），前端既有集成零改动。
    application.include_router(
        GuardedGraphQLRouter(
            build_schema(GRAPHQL_DEFAULT_VERSION),
            path=settings.graphql_path,
            context_getter=_graphql_context,
        )
    )

    @application.get("/")
    async def root() -> dict[str, str]:
        return {"message": "OK"}

    # 可观测基座：Prometheus 自动 HTTP 埋点 + /metrics 抓取端点（M0.5.1）。
    # 放装配尾部，使中间件覆盖已注册的全部路由；metrics_enabled=false 或缺依赖时 fail-open。
    setup_metrics(application)

    return application


async def _on_err(_request: Request, exc: Exception) -> JSONResponse:
    _, errcode, detail = map_err(exc)
    # BizError 可携带响应头（如 git smart-HTTP 401 的 WWW-Authenticate 挑战头）；其它异常
    # 类型没有该属性 → None，resp_json 自会按状态码补 Retry-After。
    return resp_json(errcode, detail=detail, headers=getattr(exc, "headers", None))


app = create_app()
