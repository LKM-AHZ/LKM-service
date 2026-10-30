"""AUTH 独立 ASGI 进程：认证、用户资料、内部身份读面、JWKS 与健康检查。

通过 ``uvicorn auth.main:app`` 或 ``python -m auth.main`` 启动。路由由本模块显式
聚合，不加载业务 REST、GraphQL 或 WebSocket。业务与认证组合部署的装配入口另见
``boot.backend``；app/auth 的跨包调用通过 core 端口绑定。

本进程拥有独立 auth 数据库及其生命周期：后台执行 ``init_auth_db``（开发建表或
``alembic_auth`` 迁移），初始化成功后预热用户位图；探针以 schema 就绪状态决定
readiness。Passkey 挑战清理由本进程负责，停机时释放自持后台任务与连接。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from auth import (
    admin_router,
    router_2fa,
    router_authz,
    router_bot_sso,
    router_jwks,
    router_oauth,
    router_onboarding,
    router_passkey,
    router_read,
    router_recovery,
    router_settings,
)
from auth import health as health_auth
from auth import (
    router as auth_router,
)
from auth.db.init import init_auth_db
from auth.db.session import dispose_auth_engine
from auth.service_passkey import cleanup_expired_challenges
from core import logging as app_logging
from core import metrics_relay
from core import redis as redis_client
from core.apm import init_sentry
from core.config import settings
from core.db.session import dispose_engine
from core.err import BizError, map_err, resp_json
from core.middleware import install_security_middleware
from core.tracing import setup_tracing, shutdown_tracing

logger = logging.getLogger(__name__)

# auth 库 schema 初始化失败后的指数退避区间（秒）；见 _init_auth_db_with_retry
_AUTH_INIT_DB_RETRY_MIN_S = 1.0
_AUTH_INIT_DB_RETRY_MAX_S = 30.0


async def _init_auth_db_with_retry() -> None:
    """后台初始化 auth 独立库 schema，失败按指数退避重试（启动不阻塞，与单体 app.main 对齐）。

    DB 暂时不可用时 auth 进程照常起、探针照常应答（readiness 报未就绪而非进程死掉），
    恢复后自行续上、无需重启。
    """
    delay = _AUTH_INIT_DB_RETRY_MIN_S
    while True:
        try:
            await init_auth_db()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("auth schema init failed; retry in %.0fs", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, _AUTH_INIT_DB_RETRY_MAX_S)


async def _startup_preheat_bloom() -> None:
    """建库后预热 user id 白名单位图（§5.6）。

    必须**在 auth schema 就绪之后**跑（否则 users 表还不存在）。预热成功才打出 ``seeded``
    门禁标记、拦截才生效——故这是独立 AUTH 部署不必等每日 cron 的一次性兜底；多副本重复跑
    幂等。整体 fail-open：预热失败只记日志，绝不让它拖垮进程或让拦截在半成品位图上生效。
    """
    await _init_auth_db_with_retry()
    try:
        from auth.bloom_seed import backfill_user_ids

        await backfill_user_ids()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("bloom seed at startup failed (忽略；拦截整段不生效)")


# 本进程装配的 auth 面（子集口径见模块 docstring）：独立于业务 registry，
# 显式 import 各 auth router 聚合，不触发非 auth 模块副作用。
_AUTH_ROUTERS = [
    auth_router.router,
    admin_router.router,  # S5-A2 Step0：后台 admin 会话写面（auth 域自足版，DB=独立 auth 库）
    router_2fa.router,
    router_authz.router,
    # bot-ticket 内部端点：业务进程的 user_http.mint_bot_sso_ticket 打的就是
    # {auth_http_url}{api_prefix}/auth/internal/bot-ticket，漏挂会让拆库部署恒 404。
    router_bot_sso.router,
    router_oauth.router,
    router_onboarding.router,
    router_passkey.router,
    router_read.router,  # B1.2：AUTH 进程同样 serve 内部读缝（供消费进程序扣真值/sv）
    router_recovery.router,
    router_settings.router,
]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    # 可观测基座与单体 app.main.lifespan 对齐：结构化日志（JSON 格式 + request-id/trace
    # 上下文，均幂等；Sentry DSN 为空则不加载）。不调 setup_logging 的话本进程走 root
    # logger，下面 catch-all 处理器里 map_err 打的 "Unhandled exception" 既无 JSON 结构
    # 也无 request/trace 关联，线上定位几乎无从下手。
    app_logging.setup_logging()
    init_sentry()
    init_db_task = asyncio.create_task(_startup_preheat_bloom())
    # passkey 挑战码周期清理：本进程是 auth 库的宿主（「进程=库边界」），拆库部署下这条只能由
    # 这里承担——主进程没有 auth 库连接（起不来，见 app.main.lifespan 的 seam 判断）。融合
    # 部署（无独立 AUTH 进程）仍由 app.main 起，两者按 authz 缝开关互斥，不会重复跑。
    cleanup_task = asyncio.create_task(cleanup_expired_challenges())
    # 跨进程指标中继（选项③）：本进程写 user_snap_cache_total / user_snap_singleflight_total /
    # notify_failed_total，且刻意不挂 /metrics（见 create_auth_app 说明）——快照交给 API 进程代报。
    metrics_relay.start_publisher()
    try:
        yield
    finally:
        # 收尾 schema 初始化 task：不取消它会继续访问下面已释放的引擎/连接池
        init_db_task.cancel()
        try:
            await init_db_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("auth schema init task failed during shutdown")
        # 同 init_db_task：不取消会在下面 dispose_auth_engine 之后继续用已释放的连接池。
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("passkey cleanup task failed during shutdown")
        await metrics_relay.stop_publisher()
        # 退出清理：dispose 引擎(auth 专属 + 既有业务引擎) / close redis，不泄漏连接
        shutdown_tracing()
        await dispose_auth_engine()
        await dispose_engine()
        await redis_client.close_redis()


def create_auth_app() -> FastAPI:
    """装配 auth-only ASGI 应用。不调 registry.load_all / 不起 GraphQL / 不挂 /metrics。"""
    application = FastAPI(
        title=f"{settings.app_name}-auth",
        version=settings.app_version,
        lifespan=lifespan,
    )

    install_security_middleware(application)

    setup_tracing(application, service_suffix="-auth")

    application.add_exception_handler(BizError, _on_err)
    application.add_exception_handler(RequestValidationError, _on_err)
    application.add_exception_handler(Exception, _on_err)

    # mount auth 域面 + 本进程健康面（健康面不经 api_prefix，供编排直接命中）
    for _r in _AUTH_ROUTERS:
        application.include_router(_r, prefix=settings.api_prefix)
    application.include_router(health_auth.router)
    application.include_router(router_jwks.router)

    return application


async def _on_err(_request: Request, exc: Exception) -> JSONResponse:
    _, errcode, detail = map_err(exc)
    # 同 app.main：BizError 可携带响应头（如协议端点的挑战头），其它异常类型无该属性。
    return resp_json(errcode, detail=detail, headers=getattr(exc, "headers", None))


app = create_auth_app()


def main() -> None:
    """compose `python -m auth.main` 入口：起本进程自己的 uvicorn（内网端口）。

    只跑本 ASGI app，单 worker；host 0.0.0.0 + 固定内网端口 8001，
    由 compose auth 服务 def 面内部承载（无主机端口映射），B1.2 再交由 APISIX 反代。
    """
    import uvicorn

    # 直接传已建好的 app 对象，不要用 "auth.main:app" 字符串：`python -m auth.main` 下本模块
    # 先以 __main__ 执行过一次（模块级 app = create_auth_app() 已把 _tracer_provider 建好），
    # uvicorn 再按字符串 import auth.main 会**第二次**建 app——而 setup_tracing 见 provider
    # 已存在就早退，那个被真正 serve 的 app 从未挂 FastAPIInstrumentor，HTTP server span 全丢。
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        log_level="info",
    )


if __name__ == "__main__":
    main()
