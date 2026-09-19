"""公网安全面中间件（M6.1）：TrustedHost + CORS(仅非生产) + 安全响应头。

单体（``app.main``）与 auth 独立进程（``auth.main``）共用 :func:`install_security_middleware`
一处装配，避免两套漂移——两进程都在 APISIX 之后直接承载 ``/api/*`` 与认证面，安全头语义
必须一致。

三件各自职责：

- ``TrustedHostMiddleware``:Host 头白名单（``settings.allowed_hosts_list``，``*`` = 不校验）。
  仅作纵深——对外 Host 已由 APISIX 的 ``hosts:`` 路由约束，此层挡住绕过网关直连容器端口的场景。
- ``CORSMiddleware``:**仅非生产挂载**。生产环境 CORS 的唯一权威是 APISIX
  （``deploy/apisix/apisix.yaml`` 的 ``cors`` 插件）——backend/auth/astro 在 compose 里都没有
  对外端口，生产流量必经网关，故应用层再挂一份纯属**第二个真相源**：两边取值必须人工同步，
  而 APISIX 的 schema 比 Starlette 严（``allow_credential=true`` 时四个字段都禁 ``*``），
  照抄即错（2026-09-16 就因此把 8 条路由整条拒载，见路线图 §8 #26）。本地开发前端直连
  ``:8000`` 仍需跨域，故非生产保留。
- ``SecurityHeadersMiddleware``:自研轻量 ASGI 中间件，补 nosniff / X-Frame-Options /
  Referrer-Policy / Permissions-Policy；**HSTS 仅生产**——纯 HTTP 环境误开会把域名锁死到 https。

装配顺序（``add_middleware`` 后加者在外层）：安全头最后加 → 最外层，连 TrustedHost/CORS 的
拒答响应也带上安全头。

**生产 CORS 少了应用层兜底**：新增对外路由若漏配 APISIX 的 ``cors`` 插件，将**完全没有**跨域
响应头。由 ``tests/deploy/test_apisix_config.py`` 的「每条代理到 backend/auth 的路由都必须带
cors」断言守住（回归即红）。
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

# 允许的请求方法/暴露头：方法取 REST 全集（含 OPTIONS 预检）；暴露 X-Request-ID 供前端
# 关联结构化访问日志（见 app.main 的 _log_requests）。
_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_EXPOSE_HEADERS = ["X-Request-ID"]
_MAX_AGE_S = 3600

_HSTS_VALUE = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """补安全响应头（纯 ASGI，流式响应亦覆盖）。

    仅在 ``http`` scope 上生效；WS 握手不走本层（升级后不是 HTTP 响应）。已存在同名头
    则不覆盖（留给路由/上游显式设置，如个别端点自定 CSP）。
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self._headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
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


def install_security_middleware(application: FastAPI) -> None:
    """装配安全面中间件（含生产必填校验；仅 HTTP 服务进程调用）。"""
    settings.assert_web_security_configured()

    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list
    )
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
    # 最后加 → 最外层：TrustedHost/CORS 的拒答也带安全头
    application.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)


__all__: list[str] = [
    "_HSTS_VALUE",
    "SecurityHeadersMiddleware",
    "install_security_middleware",
]
