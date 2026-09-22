"""Auth 独立进程(B1.1)的专属健康面：liveness + readiness。

与单体 ``app.modules.health.router`` 刻意分离、互不引用：
单体 /health 是经 ApiResp 包裹的模块健康；本进程是「auth-only ASGI 进程」自己治病的
轻量探活，供容器编排自洽（compose healthcheck / 后续 B1.2 APISIX 上游均可消费）：

- ``liveness``：自身存活。**零外部依赖**，仅证明进程起来能应答。
- ``readiness``：依赖就绪。聚合 DB(SELECT 1) + Redis(ping)，供 service 依赖序判定。
  细粒度复合/合并生产端点是 B1.3 的活，此处先给出干净、可被 healthcheck 单独命中的探测缝。

跨文件 import 保持极简：只依赖 ``app.core.redis`` 与 ``auth.db.session`` 的
``get_auth_engine``，均属 infra 且为 auth 进程必要的只读底座，不引业务模块。**探的是 auth
独立库**（auth 进程自持的 users/profiles 库），而非业务库——业务库 schema 不属本进程职责。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core import redis as redis_client
from auth.db.session import get_auth_engine

logger = logging.getLogger("lkm.auth.health")

router = APIRouter(tags=["auth-health"])

# DB 探活上限：引擎未配 command_timeout（app/db/session.py 建引擎时无 connect_args），
# 黑洞主机下 asyncpg 建连默认可挂 60s，探针会把 ASGI 事件循环一起拖住
_PROBE_TIMEOUT_S = 3.0


class AuthDepStatus(BaseModel):
    """依赖单项状态：up | disabled | error（disabled = 未配置/未初始化）。"""

    status: str
    detail: str | None = None


class AuthLiveData(BaseModel):
    """liveness 响应：仅证明进程存活/应答，不断言任何外部依赖。"""

    status: str
    service: str


class AuthReadyData(BaseModel):
    """readiness 响应：进程存活 + DB/Redis 依赖就绪状态。"""

    status: str
    service: str
    db: AuthDepStatus
    redis: AuthDepStatus


async def probe_db() -> AuthDepStatus:
    """探 DB：auth 专属引擎（auth.db.session.get_auth_engine）SELECT 1 校验连通。

    get_auth_engine 惰性建引擎、不会返 None（建引擎不建连接）；连接失败 → error。
    detail 只回异常类名，完整堆栈进日志：本端点通常无鉴权可达，而 asyncpg/SQLAlchemy
    的报错文本里常带 DSN/host/端口/用户名，原样回显等于对外泄露基础设施信息。
    """
    try:
        engine = get_auth_engine()
    except Exception as exc:  # 配置错误（URL 构建失败等）也按 error 回报，不 500
        logger.warning("auth readiness: 引擎构建失败", exc_info=True)
        return AuthDepStatus(status="error", detail=type(exc).__name__)

    async def _ping() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_ping(), _PROBE_TIMEOUT_S)
        return AuthDepStatus(status="up")
    except TimeoutError:
        logger.warning("auth readiness: DB 探活超时（%ss）", _PROBE_TIMEOUT_S)
        return AuthDepStatus(status="error", detail="timeout")
    except Exception as exc:  # 探活不因底层抖动 500，转为 error 状态回报
        logger.warning("auth readiness: DB 探活失败", exc_info=True)
        return AuthDepStatus(status="error", detail=type(exc).__name__)


async def probe_redis() -> AuthDepStatus:
    """探 Redis：get_redis 未配置/不可用返回 None→disabled；可用则 ping。"""
    try:
        # 与 probe_db 对称：get_redis 自身的降级路径（关连接池）也可能抛，不该让探针 500
        client = await redis_client.get_redis()
    except Exception as exc:
        logger.warning("auth readiness: redis 客户端获取失败", exc_info=True)
        return AuthDepStatus(status="error", detail=type(exc).__name__)
    if client is None:
        return AuthDepStatus(status="disabled", detail="redis_url 未配置或不可用")
    try:
        ok = await client.ping()
    except Exception as exc:
        logger.warning("auth readiness: redis ping 失败", exc_info=True)
        return AuthDepStatus(status="error", detail=type(exc).__name__)
    if not ok:
        return AuthDepStatus(status="error", detail="ping failed")
    return AuthDepStatus(status="up")


@router.get("/liveness", response_model=AuthLiveData)
async def liveness() -> AuthLiveData:
    """存活探针：零外部依赖，进程能应答即 up。供 compose/编排判断进程心跳。"""
    return AuthLiveData(status="ok", service="auth")


@router.get("/readiness", response_model=AuthReadyData)
async def readiness() -> AuthReadyData:
    """就绪探针：DB + Redis 均 up 才算 ok，否则 degraded（可被编排读作未就绪）。"""
    db = await probe_db()
    redis = await probe_redis()
    overall = "ok" if (db.status == "up" and redis.status == "up") else "degraded"
    return AuthReadyData(status=overall, service="auth", db=db, redis=redis)
