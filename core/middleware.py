"""公网安全面中间件：request_id + TrustedHost + CORS(仅非生产) + 安全响应头。
单体（``app.main``）与 auth 独立进程（``auth.main``）共用 :func:`install_security_middleware`
一处装配，避免两套漂移——两进程都在 APISIX 之后直接承载 ``/api/*`` 与认证面，安全头语义必须一致。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config import settings
from core.err import CommonErr, resp_json
from core.logging import reset_request_id, set_request_id
from core.metrics import graphql_query_rejected_total

logger = logging.getLogger(__name__)

# 允许的请求方法/暴露头：方法取 REST 全集（含 OPTIONS 预检）；暴露 X-Request-ID 供前端
# 关联结构化访问日志（见 app.main 的 _log_requests），X-API-Version 供前端/日志确认
# GraphQL 多端点版本化实际命中的版本。
_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_EXPOSE_HEADERS = ["X-Request-ID", "X-API-Version"]
_MAX_AGE_S = 3600

_HSTS_VALUE = "max-age=31536000; includeSubDomains"

# 入站 X-Request-ID 的采纳上限：超长会撑爆日志/响应头（h11 对头部字节数有限制）。
_MAX_REQUEST_ID_LEN = 128


class RequestIdMiddleware:
    """
    请求级 request_id 注入（纯 ASGI，两进程共用）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        candidate = Headers(scope=scope).get("X-Request-ID") or ""
        request_id = (
            candidate
            if candidate
            and candidate.isascii()
            and candidate.isprintable()
            and len(candidate) <= _MAX_REQUEST_ID_LEN
            else uuid.uuid4().hex
        )
        token = set_request_id(request_id)

        async def _send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, _send_with_id)
        finally:
            reset_request_id(token)


class SecurityHeadersMiddleware:
    """
    补安全响应头（纯 ASGI，流式响应亦覆盖）。
    仅在 ``http`` scope 上生效；WS 握手不走本层（升级后不是 HTTP 响应）。已存在同名头
    则不覆盖（留给路由/上游显式设置，如个别端点自定 CSP）。
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self._headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", b"frame-ancestors 'none'"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
        ]
        if hsts:
            self._headers.append(
                (b"strict-transport-security", _HSTS_VALUE.encode("latin-1"))
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self._headers:
                    headers.setdefault(key.decode("latin-1"), value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, _send_with_headers)


class GraphQLHTTPMiddleware:
    """
    GraphQL 端点的 HTTP 边缘关切（§2）：查询级**硬**超时 + 版本响应头。
    只对 ``settings.graphql_path`` 下的请求生效，其余请求零开销直通。
    **版本响应头**：**只**在端点路径显式带版本（``/graphql/v1``）时写 ``X-API-Version``，
    无版本的别名路径（``/graphql``）不写——不替调用方猜「这条别名现在等价于哪个版本」。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        prefix = settings.graphql_path.rstrip("/")
        if path != prefix and not path.startswith(f"{prefix}/"):
            await self.app(scope, receive, send)
            return

        buffered: list[Message] = []

        async def _buffer(message: Message) -> None:
            buffered.append(message)

        try:
            await asyncio.wait_for(
                self.app(scope, receive, _buffer), settings.graphql_hard_timeout_s
            )
        except TimeoutError:
            graphql_query_rejected_total.labels("timeout").inc()
            logger.warning(
                "GraphQL 请求超出 %.1fs 硬超时，已中断其执行",
                settings.graphql_hard_timeout_s,
            )
            await _send_graphql_timeout(send, scope)
            return

        version = _explicit_graphql_version(scope)
        for message in buffered:
            if message["type"] == "http.response.start" and version:
                MutableHeaders(scope=message).setdefault("X-API-Version", version)
            await send(message)


def _explicit_graphql_version(scope: Scope) -> str:
    """端点路径里显式声明的版本号（``/graphql/v1`` → ``v1``）；别名路径返回空串。"""
    prefix = settings.graphql_path.rstrip("/")
    template = getattr(scope.get("route"), "path", "") or scope.get("path", "")
    if template != prefix and not template.startswith(f"{prefix}/"):
        return ""
    return template[len(prefix) :].strip("/").split("/")[0]


async def _send_graphql_timeout(send: Send, scope: Scope) -> None:
    """产出 504 信封。走 ``resp_json`` 而非手拼 JSON——信封字段名/文案只此一处定义。"""
    response = resp_json(CommonErr.TIMEOUT)
    headers = [(k.lower(), v) for k, v in response.headers.raw]
    version = _explicit_graphql_version(scope)
    if version:
        headers.append((b"x-api-version", version.encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": response.status_code,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": response.body})


def install_security_middleware(application: FastAPI) -> None:
    """装配安全面中间件（含生产必填校验；仅 HTTP 服务进程调用）。"""
    settings.assert_web_security_configured()

    allowed_hosts = settings.allowed_hosts_list
    if settings.is_production and "*" in allowed_hosts:
        raise ValueError(
            "生产禁用通配 LKM_ALLOWED_HOSTS=*：会使 TrustedHost 校验整体失效"
        )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    # CORS 只在非生产挂载：生产唯一权威是 APISIX（见模块 docstring 的取舍说明）
    if not settings.is_production:
        origins = settings.cors_origins_list
        # 禁「* + 凭证」并存：命中通配即关闭 credentials（跨域读仍可用，但不再携带 cookie）。
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials="*" not in origins,
            allow_methods=_ALLOW_METHODS,
            allow_headers=["*"],
            expose_headers=_EXPOSE_HEADERS,
            max_age=_MAX_AGE_S,
        )
    # 安全头先加 → 外层：TrustedHost/CORS 的拒答也带安全头
    application.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    application.add_middleware(RequestIdMiddleware)


__all__: list[str] = [
    "_HSTS_VALUE",
    "GraphQLHTTPMiddleware",
    "RequestIdMiddleware",
    "SecurityHeadersMiddleware",
    "install_security_middleware",
]
