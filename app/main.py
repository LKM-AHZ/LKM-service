import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response
from strawberry.fastapi import BaseContext, GraphQLRouter

from app.api.graphql import build_schema
from app.api.router import api_router
from app.core import logging as logger
from app.core import messaging, user_cache_events
from app.core import redis as redis_client
from app.core.apm import init_sentry
from app.core.config import settings
from app.core.err import BizError, map_err, resp_json
from app.core.metrics import setup_metrics
from app.core.pulsar_lag import start_lag_reporter, stop_lag_reporter
from app.core.tracing import (
    instrument_sqlalchemy,
    setup_tracing,
    shutdown_tracing,
)
from app.db.init_db import init_db
from app.db.session import (
    AsyncSession,
    dispose_engine,
    get_async_engine,
)
from app.db.session import (
    get_read_session as get_graphql_session,  # GraphQL 仅 Query(纯读)，避免空提交
)
from app.modules import registry
from app.modules.auth.deps import CurrentUser, get_optional_user
from app.modules.auth.service_passkey import cleanup_expired_challenges
from app.ws.manager import manager


@dataclass
class GraphQLContext(BaseContext):
    """GraphQL 请求上下文：持有当前请求的数据库会话（只读查询）。

    会话由 GraphQLRouter 的 context_getter 经 FastAPI 依赖注入，与 REST 端点共用
    同一会话依赖，便于测试 override。讨论帖查询已统一由 content 模块承载。
    """

    db: AsyncSession
    user_id: int | None = None


request_logger = logging.getLogger("lkm.http")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    # 可观测基座：结构化日志 + Sentry APM（均幂等；DSN 空则不加载）
    logger.setup_logging()
    init_sentry()
    # 链路追踪（M5 7.2.2）：默认关；开启时埋 FastAPI/httpx，SQLAlchemy 待引擎建好再挂
    setup_tracing(_app)

    await init_db()
    instrument_sqlalchemy(get_async_engine())

    # 启动即探测 Redis，便于日志暴露其状态（未配置/不可用时静默降级为 None）
    await redis_client.get_redis()

    # L1 本地缓存失效广播订阅（Redis 未配置则空转退避，不阻塞启动）
    await user_cache_events.start()

    cleanup_task = asyncio.create_task(cleanup_expired_challenges())

    # 可观测（M4）：Pulsar 订阅 lag 周期上报（未配置则 no-op）
    start_lag_reporter()

    yield

    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup_task

    # 收尾 WebSocket 事件的 Redis 订阅 task，避免泄漏连接
    await manager.close()

    # 收尾 L1 失效广播订阅 task（须在 close_redis 前，避免关连接竞态）
    await user_cache_events.stop()

    # 收尾 Pulsar lag 上报、producer/client（若曾发布过），避免连接泄漏
    await stop_lag_reporter()
    await messaging.close()
    await redis_client.close_redis()
    # 收尾链路追踪（限时 flush），先于引擎释放
    shutdown_tracing()
    await dispose_engine()


def create_app() -> FastAPI:
    # 聚合装配：registry.load_all() 触发各模块错误码注册（防漏配导致 500）
    registry.load_all()

    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def _log_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """结构化访问日志：注入 request_id，记录 method/route/status/latency。"""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = logger.set_request_id(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            latency_ms = (time.perf_counter() - start) * 1000
            response.headers.setdefault("X-Request-ID", request_id)
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
        finally:
            logger.reset_request_id(token)

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

    merged_schema = build_schema()  # §7：registry 聚合全部模块 GraphQL Query
    graphql_router = GraphQLRouter(
        merged_schema,
        path="/graphql",
        context_getter=_graphql_context,
    )
    application.include_router(graphql_router)

    @application.get("/")
    async def root() -> dict[str, str]:
        return {"message": "OK"}

    # 可观测基座：Prometheus 自动 HTTP 埋点 + /metrics 抓取端点（M0.5.1）。
    # 放装配尾部，使中间件覆盖已注册的全部路由；metrics_enabled=false 或缺依赖时 fail-open。
    setup_metrics(application)

    return application


async def _on_err(_request: Request, exc: Exception) -> JSONResponse:
    _, errcode, detail = map_err(exc)
    return resp_json(errcode, detail=detail)


app = create_app()
