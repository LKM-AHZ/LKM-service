import logging
from typing import Any

import httpx
from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.core import redis as redis_client
from app.core.common import ApiResp
from app.core.config import settings
from app.core.err import respond
from app.core.pulsar_lag import probe_health as probe_pulsar_health
from app.db.session import get_async_engine

router = APIRouter(tags=["health"])


class DependencyStatus(BaseModel):
    status: str
    detail: str | None = None


class HealthData(BaseModel):
    status: str
    db: DependencyStatus
    redis: DependencyStatus
    auth: DependencyStatus


class LiveData(BaseModel):
    """liveness 响应：仅证明进程存活/应答，不断言任何外部依赖。"""

    status: str
    service: str


class ReadyData(BaseModel):
    """readiness 响应：复合硬依赖（DB/Redis/Pulsar/AUTH）状态。"""

    status: str
    service: str
    db: DependencyStatus
    redis: DependencyStatus
    pulsar: DependencyStatus
    auth: DependencyStatus


# AUTH 活性的可注入出站 client 工厂（monolith 就绪探针用）：默认 None → 按配置超时新建
# httpx client 打 AUTH 进程 /liveness；测试经 monkeypatch 换成返回假 transport 的 client
# 即可离线端到端驱动（与 auth.user_http._client_factory 同款范式）。
_auth_liveness_factory: Any = None

logger = logging.getLogger(__name__)


async def _probe_auth() -> DependencyStatus:
    """AUTH 依赖可选探针（M3 B1.3）：仅当配置了 ``auth_http_url`` 才探，否则 disabled。

    单进程默认(auth_http_url 为空) → disabled 且不影响 overall —— monolith 与 AUTH 同进程
    部署时本体不声明任何对外 auth 依赖，就绪语义与既存完全一致。配置了(独立 AUTH 进程反代
    接出)才反映 AUTH 可达性：GET ``<auth_http_url>/liveness``（AUTH 自足存活端点，零级联其
    DB/Redis），失败/超时 → error(降级)。超时受配置 ``auth_http_timeout_s`` 上界约束，绝不让
    一个不可达的 AUTH 把 monolith 就绪探针挂死在连接等待上；本模块的 liveness 面不受影响。
    """
    base = (settings.auth_http_url or "").strip().rstrip("/")
    if not base:
        return DependencyStatus(status="disabled", detail="auth_http_url 未配置")
    url = f"{base}/liveness"
    try:
        async with _build_auth_client() as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        # 细节只进日志：/health、/readiness 都是**匿名**可读端点，而驱动层异常文本常带
        # 内网 host:port / 库名 / 账号，等于把基础设施信息白送给任何调用者。
        logger.warning("health probe auth failed: %s", exc)
        return DependencyStatus(status="error", detail="auth unreachable")
    if resp.status_code != 200:
        return DependencyStatus(
            status="error", detail=f"auth /liveness http {resp.status_code}"
        )
    payload = _coerce_liveness(resp)
    ok = isinstance(payload, dict) and payload.get("status") == "ok"
    if not ok:
        return DependencyStatus(status="error", detail="auth /liveness 未返回 ok")
    return DependencyStatus(status="up")


def _build_auth_client() -> httpx.AsyncClient:
    """每探测级 client + 配置超时（与 auth.user_http._build_client 同款出站风格）。"""
    if _auth_liveness_factory is not None:
        return _auth_liveness_factory()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.auth_http_timeout_s,
            read=settings.auth_http_timeout_s,
            write=settings.auth_http_timeout_s,
            pool=settings.auth_http_timeout_s,
        )
    )


def _coerce_liveness(resp: httpx.Response) -> dict[str, object] | None:
    """/liveness 响应 → dict；非 JSON/非对象 → None（探针把其当作非 ok，不抛）。"""
    try:
        parsed = resp.json()
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _probe_db() -> DependencyStatus:
    """探测数据库：执行 SELECT 1，失败返回 error（detail 固定文案，细节只进日志）。"""
    engine = get_async_engine()
    if engine is None:
        return DependencyStatus(status="error", detail="engine not initialized")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return DependencyStatus(status="up")
    except Exception as exc:
        # 匿名可读的就绪面上不回显驱动异常（常含内网 host:port / 库名 / 用户名）
        logger.warning("health probe db failed: %s", exc)
        return DependencyStatus(status="error", detail="database unreachable")


async def _probe_redis() -> DependencyStatus:
    """探测 Redis：get_redis 未配置/不可用返回 None → disabled；可用则 up。"""
    client = await redis_client.get_redis()
    if client is None:
        return DependencyStatus(status="disabled", detail="redis_url 未配置或不可用")
    try:
        ok = await client.ping()
    except Exception as exc:
        logger.warning("health probe redis failed: %s", exc)
        return DependencyStatus(status="error", detail="redis ping failed")
    if not ok:
        return DependencyStatus(status="error", detail="ping failed")
    return DependencyStatus(status="up")


async def _probe_pulsar() -> DependencyStatus:
    """探消息总线：复用 lag 上报的 Admin REST 通道（短超时 + up 结果短缓存）。

    未启用消息总线/未配 ``pulsar_admin_url`` → ``disabled``（**不计入** readiness 硬依赖，
    单机或尚未接总线的部署就绪语义明确）；否则 ``up``/``error``。
    """
    status, detail = await probe_pulsar_health()
    return DependencyStatus(status=status, detail=detail)


@router.get("/liveness", response_model=LiveData)
async def liveness() -> LiveData:
    """存活探针：**零外部依赖**，进程能应答即 ok。

    供 compose/编排判断"进程是否该被重启"——DB/Redis/Pulsar/AUTH 抖动会让 readiness
    未就绪，但不该导致容器被判死重启（见 /readiness）。
    """
    return LiveData(status="ok", service="api")


@router.get("/readiness", response_model=ReadyData)
async def readiness(response: Response) -> ReadyData:
    """就绪探针：DB + Redis + Pulsar + AUTH 复合（AND 语义），未就绪返回 **503**。

    - 硬依赖：DB/Redis 必须 ``up``；Pulsar/AUTH 已配置时必须 ``up``。
    - ``disabled``（未配置，如单机无总线/未接 AUTH 进程）不降就绪——是部署取向而非故障。
    - 状态码语义：就绪 200 / 未就绪 503（供 compose depends_on、K8s readinessProbe、
      负载均衡摘流直接消费；M3.4 已把语义定在 AUTH 进程侧，此处对齐到单体）。
    """
    db = await _probe_db()
    redis = await _probe_redis()
    pulsar = await _probe_pulsar()
    auth = await _probe_auth()
    ready = (
        db.status == "up"
        and redis.status == "up"
        and pulsar.status in ("up", "disabled")
        and auth.status in ("up", "disabled")
    )
    if not ready:
        response.status_code = 503
    return ReadyData(
        status="ok" if ready else "degraded",
        service="api",
        db=db,
        redis=redis,
        pulsar=pulsar,
        auth=auth,
    )


@router.get("/health", response_model=ApiResp[HealthData])
@respond
async def health_check() -> dict[str, object]:
    """健康检查：聚合 DB、Redis 与(可选)AUTH 状态，供探活与可观测基座使用。

    AUTH 探针仅在配置 ``auth_http_url``(独立 AUTH 进程接出)时参与降级判定；默认空 →
    ``auth.disabled`` 不参与，overall 只取决于 DB+Redis，保持既存单进程就绪语义零变化。

    注意：本端点**刻意不探 Pulsar**——它是「进程+依赖是否可用」的粗粒度视图，而 Pulsar
    是 `/readiness` 的接流硬依赖。Pulsar 不可达时两者判定会不同（此处 ok / readiness 503），
    属预期分工：要判断能否接流请看 `/readiness`，不要用本端点做接流判据。
    """
    db_status = await _probe_db()
    redis_status = await _probe_redis()
    auth_status = await _probe_auth()
    overall = (
        "ok"
        if db_status.status == "up"
        and redis_status.status == "up"
        and (auth_status.status == "up" or auth_status.status == "disabled")
        else "degraded"
    )
    return {
        "status": overall,
        "db": db_status,
        "redis": redis_status,
        "auth": auth_status,
    }
