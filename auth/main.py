"""B1.1 AUTH 独立 ASGI 进程入口：auth 域自有、与单体并行的读/认证面。

背景（M3 B1）：A1-A7 已把 auth 身份读面收敛进 monolith 内的 ``auth.snapshot`` 读缝
并给出 ``user:snap`` 缓存；B1 把该 AUTH 读对外化为独立进程的第一步 —— 先给一个
**可独立运行的 auth-only ASGI 应用** + 它**专属的 liveness/readiness 健康缝** + compose
同镜像异 command 的 ``auth`` 服务。纯增量，不改单体 ``app.main`` 任何行为。
B1.2 在同进程再挂 internal 读缝 router（``auth.router_read``，/auth/internal，B1.2 seam），
令 AUTH 进程本身也能 serve 该读端点供消费进程经 HTTP 跨缝（APISIX 路由 B1.3 接）。

装配口径：此进程只 mount ``auth`` 自己的 router（登录/档案/2FA/oauth/
passkey/recovery/settings/admin/内部读面/JWKS）**plus** 本进程自己的 auth-health router；不引业务域
（content/feed/points/projects 等）、不起 GraphQL/WebSocket、不做制品迁移 —— auth 是
owner-leaf，其 router 只依赖 core + db.session + auth 内部，故可干净独立装配。
进程内 HTTP 面对外暴露（APISIX 路由 / B1.3 复合就绪合并）属后续 leg，此处仅就绪
一个可被 compose/后续编排单独探活的自足进程。

同名但独立：DB 引擎/Redis client 由本进程内 realm 各自 lazy 自持（同库同数据源，
进程隔离）。lifespan 负责**本进程自持的 auth 独立库 schema 初始化**（``init_auth_db``：
非 alembic 走 AuthBase.create_all，alembic 走 ``alembic_auth`` 第二链）——auth 表已物理迁出
单体 ``Base.metadata``，单体不再建它们，故由 auth 进程按「进程=库边界」自建，不再是「与单体
并发迁移」（业务库迁移仍归 backend 进程）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core import logging as app_logging
from app.core import redis as redis_client
from app.core.apm import init_sentry
from app.core.config import settings
from app.core.err import BizError, map_err, resp_json
from app.core.middleware import install_security_middleware
from app.core.tracing import setup_tracing, shutdown_tracing
from app.db.session import dispose_engine
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
    # 链路追踪在 create_auth_app 装配期挂载（不能放 lifespan：中间件栈已定型，
    # instrument 不生效、HTTP span 采不到；详见 core.tracing.setup_tracing 说明）
    try:
        # 启动：初始化本进程自持的 auth 独立库 schema（AuthBase）。auth 表已迁出单体
        # Base.metadata，无其他进程会建它们，故是 auth 进程的职责；业务库 schema 仍由
        # backend 进程的 init_db 负责，二者分库、各自的 Alembic 链与迁移锁互不干扰。
        # 放进 try：schema 初始化失败时 finally 仍会跑清理（tracing/引擎/redis），
        # 否则启动失败会连清理一起跳过。
        await init_auth_db()
        yield
    finally:
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

    # 公网安全面（M6.1）：与单体共用同一装配（Host 白名单 / CORS 白名单 / 安全头，
    # HSTS 仅生产）——auth 承载 /api/v1/auth/* 对外面，语义须与 backend 一致。
    install_security_middleware(application)

    # 链路追踪（M5 7.2.2）：auth 进程独立 service 名；**装配期**挂载（见 lifespan 说明）
    setup_tracing(application, service_suffix="-auth")

    # 与单体一致的 error 语义映射（BizError / 校验错误 / 兜底 500），保证 auth 端点错误口径一致
    application.add_exception_handler(BizError, _on_err)
    application.add_exception_handler(RequestValidationError, _on_err)
    application.add_exception_handler(Exception, _on_err)

    # mount auth 域面 + 本进程健康面（健康面不经 api_prefix，供编排直接命中）
    for _r in _AUTH_ROUTERS:
        application.include_router(_r, prefix=settings.api_prefix)
    application.include_router(health_auth.router)
    # JWKS（批 5）：规范位置在站点根，故不走 api_prefix
    application.include_router(router_jwks.router)

    return application


async def _on_err(_request: Request, exc: Exception) -> JSONResponse:
    _, errcode, detail = map_err(exc)
    return resp_json(errcode, detail=detail)


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
