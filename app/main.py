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

from app.api.graphql import GuardedGraphQLRouter, build_schema
from app.api.router import api_router
from app.core import clickhouse, messaging, user_cache_events
from app.core import logging as logger
from app.core import redis as redis_client
from app.core.apm import init_sentry
from app.core.config import settings
from app.core.err import BizError, map_err, resp_json
from app.core.metrics import setup_metrics
from app.core.middleware import install_security_middleware
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
from app.ws.manager import manager
from auth.deps import CurrentUser, get_optional_user
from auth.seams import cleanup_expired_challenges


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


# schema 初始化失败后的指数退避区间（秒）：由下面的后台 task 承担，见 _init_db_with_retry
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
    # 链路追踪（M5 7.2.2）在 create_app 装配期挂载——不能放 lifespan：Starlette 处理
    # lifespan 请求时中间件栈已定型，此处再 instrument 不会生效，HTTP server span 采不到
    # （SQLAlchemy/httpx 埋点不依赖中间件栈，会照常工作而掩盖问题）。SQLAlchemy 埋点须等
    # 引擎建好，故仍留在此处。
    # 启动不阻塞（§2 第 1 条）：schema 初始化放后台重试、不 await——DB 未就绪时进程仍要起来
    # 并经 readiness 如实报「未就绪」，而不是起不来进 crashloop。就绪判定见 init_db 的完成标志。
    init_db_task = asyncio.create_task(_init_db_with_retry())
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
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    except Exception:
        # 非取消类异常不得打断收尾：它会使后续资源全部泄漏
        request_logger.exception("background cleanup task failed during shutdown")

    # 收尾 schema 初始化重试 task：不取消的话它会继续访问下面已释放的引擎/连接池
    init_db_task.cancel()
    try:
        await init_db_task
    except asyncio.CancelledError:
        pass
    except Exception:
        request_logger.exception("schema init task failed during shutdown")

    # 逐步骤兜底（顺序不变）：任一 close 抛错都不能跳过其余释放，否则连接泄漏
    # 收尾 WebSocket 事件的 Redis 订阅 task，避免泄漏连接
    await _shutdown_step("ws_manager", manager.close)
    # 收尾 L1 失效广播订阅 task（须在 close_redis 前，避免关连接竞态）
    await _shutdown_step("user_cache_events", user_cache_events.stop)
    # 收尾 Pulsar lag 上报、producer/client（若曾发布过），避免连接泄漏
    await _shutdown_step("pulsar_lag", stop_lag_reporter)
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
        # 客户端传来的 X-Request-ID 不可直接采信：CR/LF 等控制字符会造成日志伪造/响应头
        # 注入（h11 会直接拒答），非 ASCII 也会污染日志；不合规就另生成一个
        candidate_id = request.headers.get("X-Request-ID") or ""
        request_id = (
            candidate_id
            if candidate_id
            and candidate_id.isascii()
            and candidate_id.isprintable()
            and len(candidate_id) <= 128
            else uuid.uuid4().hex
        )
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
        finally:
            logger.reset_request_id(token)

    # 公网安全面（M6.1）：TrustedHost + CORS 白名单 + 安全响应头。**最后加 → 最外层**，
    # 使访问日志与其下全部业务路由、以及 TrustedHost/CORS 的拒答响应都带上安全头。
    # 生产缺 LKM_ALLOWED_HOSTS/LKM_CORS_ORIGINS 时在此 fail-fast（不静默降级）。
    install_security_middleware(application)

    # 链路追踪（M5 7.2.2）：**装配期**挂载，须在返回 app 前——见 lifespan 顶部说明。
    # 放在安全中间件之后 → OTel 成为最外层中间件，span 覆盖整个请求处理链。
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

    merged_schema = build_schema()  # §7：registry 聚合全部模块 GraphQL Query
    graphql_router = GuardedGraphQLRouter(
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
