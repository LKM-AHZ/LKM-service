import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from core import redis as redis_client
from core.common import ApiResp
from core.config import settings
from core.db.init_db import is_db_initialized
from core.db.session import get_async_engine
from core.err import respond
from core.ports.verify_keys import refresh_verify_key, verify_key_status
from core.pulsar_lag import probe_health as probe_pulsar_health

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


class SoftDependencies(BaseModel):
    """软依赖状态（蓝图 §2 第 3 条）：**缺失可降级，只告知不阻塞入流**。

    与硬依赖的关键区别：这些字段**绝不参与** readiness 的 200/503 判定，只是把
    「检索/对象存储当前是否可用」的信息随就绪响应一并告知，供运维观测与客户端降级决策。
    """

    search: DependencyStatus
    storage: DependencyStatus


class ReadyData(BaseModel):
    """readiness 响应：复合硬依赖（DB/Redis/Pulsar/AUTH/验签公钥）状态 + 软依赖告知。"""

    status: str
    service: str
    db: DependencyStatus
    redis: DependencyStatus
    pulsar: DependencyStatus
    auth: DependencyStatus
    # 验签公钥（蓝图 §2 第 2 条）：RS256 部署下这是承接带 token 流量的前置条件，须如实上报。
    verify_key: DependencyStatus
    soft: SoftDependencies


# AUTH 活性的可注入出站 client 工厂（monolith 就绪探针用）：默认 None → 按配置超时新建
# httpx client 打 AUTH 进程 /liveness；测试经 monkeypatch 换成返回假 transport 的 client
# 即可离线端到端驱动（与 auth.user_http._client_factory 同款范式）。
_auth_liveness_factory: Any = None

# 软依赖探测的出站 client 工厂（可注入，测试离线驱动用；语义同 _auth_liveness_factory）。
# 软依赖只告知不阻塞，故**不**引入配置项，超时用模块常量。
_soft_probe_factory: Any = None

# 软依赖探测超时（秒）：readiness 可能被编排高频调用，探测必须是「轻量 + 短超时」，绝不能
# 让一个不可达的 OpenSearch/MinIO 把就绪探针拖住（即便不改变判定，也会拖慢响应）。
_SOFT_PROBE_TIMEOUT_S = 2.0

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
    """探测数据库：**schema 已初始化** 且 SELECT 1 通过，才算 up。

    只探连通性不够：DB 可达但表/迁移尚未建好时 `SELECT 1` 照样成功，readiness 会误报 up，
    把流量放进一个查不了业务表的进程——启动不阻塞（lifespan 不再 await init_db）之后这个
    窗口是真实存在的。故先看进程内的初始化完成标志（见 ``core.db.init_db.is_db_initialized``）。
    """
    if not is_db_initialized():
        return DependencyStatus(status="error", detail="schema not initialized")
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
    """探测**所有已配置的** Redis 后端：一个都没配 → disabled；任一不可用 → error；全通 → up。

    双后端并行时任一后端不可用都算降级——它承载的那部分域会退到 fail-open。
    ``all_clients()`` 只返回「可用」的后端，故必须与「已配置数量」比对才能发现掉线。
    """
    expected = 1 + (1 if redis_client.secondary_configured() else 0)
    clients = await redis_client.all_clients()
    if not clients:
        return DependencyStatus(status="disabled", detail="redis_url 未配置或不可用")
    if len(clients) < expected:
        return DependencyStatus(
            status="error", detail=f"redis 后端不可用（{len(clients)}/{expected}）"
        )
    for _label, client in clients:
        try:
            ok = await client.ping()
        except Exception as exc:
            logger.warning("health probe redis failed: %s", exc)
            return DependencyStatus(status="error", detail="redis ping failed")
        if not ok:
            return DependencyStatus(status="error", detail="ping failed")
    return DependencyStatus(status="up")


async def _probe_verify_key() -> DependencyStatus:
    """硬依赖（§2 第 2 条）：JWT 验签公钥是否可用。

    - 本地有公钥（env / ``*_file``）、或运行期已从 AUTH JWKS 拉到 → ``up``；
    - 拿不到公钥 → **就地尝试一次** JWKS 拉取（自愈：AUTH 晚于本进程起来也能接上），
      仍失败才 ``error``。detail 刻意不含内网信息（本端点是匿名可读的）。
    """
    if verify_key_status() == "ok":
        return DependencyStatus(status="up")
    if await refresh_verify_key():
        return DependencyStatus(status="up")
    return DependencyStatus(status="error", detail="verify key unavailable")


async def _probe_pulsar() -> DependencyStatus:
    """探消息总线：复用 lag 上报的 Admin REST 通道（短超时 + up 结果短缓存）。

    未启用消息总线/未配 ``pulsar_admin_url`` → ``disabled``（**不计入** readiness 硬依赖，
    单机或尚未接总线的部署就绪语义明确）；否则 ``up``/``error``。
    """
    status, detail = await probe_pulsar_health()
    return DependencyStatus(status=status, detail=detail)


def _build_soft_client() -> httpx.AsyncClient:
    """软探测的轻量 client：可注入工厂（测试离线驱动），默认按模块常量超时新建。"""
    if _soft_probe_factory is not None:
        return _soft_probe_factory()
    timeout = httpx.Timeout(
        connect=_SOFT_PROBE_TIMEOUT_S,
        read=_SOFT_PROBE_TIMEOUT_S,
        write=_SOFT_PROBE_TIMEOUT_S,
        pool=_SOFT_PROBE_TIMEOUT_S,
    )
    return httpx.AsyncClient(timeout=timeout)


async def _probe_search() -> DependencyStatus:
    """软依赖：检索后端（可降级，只告知不阻塞）。

    - 内置 ``pg`` 检索（默认）/ 其它未接出的引擎 → ``disabled``：无外部依赖。
    - ``opensearch`` 且配了 URL：轻量 ``GET /_cluster/health``（短超时）→ ``up``/``error``。
    结果**不参与** readiness 判定，异常只记日志、不冒泡。
    """
    engine = settings.search_engine
    base = (settings.search_opensearch_url or "").strip().rstrip("/")
    if engine != "opensearch":
        return DependencyStatus(status="disabled", detail=f"search_engine={engine}")
    if not base:
        return DependencyStatus(status="disabled", detail="opensearch url 未配置")
    try:
        async with _build_soft_client() as client:
            resp = await client.get(f"{base}/_cluster/health")
    except httpx.HTTPError as exc:
        logger.warning("health probe search failed: %s", exc)
        return DependencyStatus(status="error", detail="search unreachable")
    if resp.status_code >= 500:
        return DependencyStatus(status="error", detail=f"search http {resp.status_code}")
    return DependencyStatus(status="up")


async def _probe_storage() -> DependencyStatus:
    """软依赖：对象存储（可降级，只告知不阻塞）。

    - ``local`` 后端（默认）→ ``disabled``：文件落本地，无外部依赖。
    - ``s3`` 且配了显式 endpoint（MinIO 本地）：轻量 ``GET <endpoint>/`` 探可达；
      S3 对匿名/根路径常回 403/404，**只要拿到 HTTP 响应即视为可达**（``up``）。
    - ``s3`` 但未配 endpoint（云 S3 默认 endpoint）：不主动探测，报 ``disabled``，避免为
      readiness 引入一个隐式外部调用。结果同样**不参与** readiness 判定。
    """
    backend = (settings.storage_backend or "local").strip()
    if backend != "s3":
        return DependencyStatus(status="disabled", detail=f"storage_backend={backend}")
    base = (settings.s3_endpoint_url or "").strip().rstrip("/")
    if not base:
        return DependencyStatus(status="disabled", detail="s3 默认 endpoint 未探测")
    try:
        async with _build_soft_client() as client:
            resp = await client.get(base)
    except httpx.HTTPError as exc:
        logger.warning("health probe storage failed: %s", exc)
        return DependencyStatus(status="error", detail="storage unreachable")
    return DependencyStatus(status="up", detail=f"http {resp.status_code}")


@router.get("/liveness", response_model=LiveData)
async def liveness() -> LiveData:
    """存活探针：**零外部依赖**，进程能应答即 ok。

    供 compose/编排判断"进程是否该被重启"——DB/Redis/Pulsar/AUTH 抖动会让 readiness
    未就绪，但不该导致容器被判死重启（见 /readiness）。
    """
    return LiveData(status="ok", service="api")


@router.get("/readiness", response_model=ReadyData)
async def readiness(response: Response) -> ReadyData:
    """就绪探针：DB + Redis + Pulsar + AUTH + **验签公钥** 复合（AND 语义），未就绪返回 **503**。

    - 硬依赖：DB/Redis 必须 ``up``；Pulsar/AUTH 已配置时必须 ``up``；**验签公钥**必须可用
      （RS256-only，见 ``_probe_verify_key``）。
    - ``disabled``（未配置，如单机无总线/未接 AUTH 进程）不降就绪——是部署取向而非故障。
    - **软依赖**（检索/对象存储，蓝图 §2 第 3 条）：仅作 ``soft`` 字段告知，**不参与**下述
      ``ready`` 判定——它们缺失时可降级，绝不能把进程挡在入流之外。
    - 状态码语义：就绪 200 / 未就绪 503（供 compose depends_on、K8s readinessProbe、
      负载均衡摘流直接消费；M3.4 已把语义定在 AUTH 进程侧，此处对齐到单体）。
    """
    db = await _probe_db()
    redis = await _probe_redis()
    pulsar = await _probe_pulsar()
    auth = await _probe_auth()
    verify_key = await _probe_verify_key()
    # 软依赖：探测失败只体现在字段值，绝不改变下面 ready 的 AND 判定（一字不动）。
    # 并发探测把最坏延迟收敛到单个超时（~2s），而非两者串行叠加。
    search, storage = await asyncio.gather(_probe_search(), _probe_storage())
    soft = SoftDependencies(search=search, storage=storage)
    ready = (
        db.status == "up"
        and redis.status == "up"
        and pulsar.status in ("up", "disabled")
        and auth.status in ("up", "disabled")
        and verify_key.status == "up"
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
        verify_key=verify_key,
        soft=soft,
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
