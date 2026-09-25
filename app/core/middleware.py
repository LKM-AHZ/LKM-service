"""公网安全面中间件（M6.1）：request_id + TrustedHost + CORS(仅非生产) + 安全响应头。

单体（``app.main``）与 auth 独立进程（``auth.main``）共用 :func:`install_security_middleware`
一处装配，避免两套漂移——两进程都在 APISIX 之后直接承载 ``/api/*`` 与认证面，安全头语义
必须一致。

四件各自职责：

- ``RequestIdMiddleware``:请求级 request_id 注入（读/净化入站 ``X-Request-ID``，写 ContextVar
  供 JSON 日志与**响应信封**读取，并回写响应头）。原先这段逻辑只活在 ``app.main._log_requests``
  里，auth 进程没有对应中间件 → auth 的响应既无 ``X-Request-ID`` 头、也无 request_id 日志关联；
  收拢在此处后两进程天然一致。

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

装配顺序（``add_middleware`` 后加者在外层）：request_id 最后加 → 这一组里最外层，
连 TrustedHost/CORS 的拒答响应也带上 ``X-Request-ID``；安全头紧随其内，同样覆盖那些拒答。

**「最外层」的边界**：只在**用户中间件栈内**成立。Starlette 的 ``ServerErrorMiddleware`` 位于
所有用户中间件之外，而 ``add_exception_handler(Exception, _on_err)``（``app.main`` /
``auth.main``）注册的兜底处理器正是挂在它上面——未捕获异常的 500 响应直接写 transport，
**不**经本层：既无 nosniff/X-Frame-Options/Referrer-Policy/HSTS，非生产下也无 CORS 头
（浏览器会把它显示成跨域失败）。要覆盖该路径需自建错误中间件或改注册方式。

**生产 CORS 少了应用层兜底**：新增对外路由若漏配 APISIX 的 ``cors`` 插件，将**完全没有**跨域
响应头。由 ``tests/deploy/test_apisix_config.py`` 的「每条代理到 backend/auth 的路由都必须带
cors」断言守住（回归即红）。
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

from app.core.config import settings
from app.core.err import CommonErr, resp_json
from app.core.logging import reset_request_id, set_request_id
from app.core.metrics import graphql_query_rejected_total

logger = logging.getLogger(__name__)

# 允许的请求方法/暴露头：方法取 REST 全集（含 OPTIONS 预检）；暴露 X-Request-ID 供前端
# 关联结构化访问日志（见 app.main 的 _log_requests），X-API-Version 供前端/日志确认
# GraphQL 多端点版本化实际命中的版本（§2）。
_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_EXPOSE_HEADERS = ["X-Request-ID", "X-API-Version"]
_MAX_AGE_S = 3600

_HSTS_VALUE = "max-age=31536000; includeSubDomains"

# 入站 X-Request-ID 的采纳上限：超长会撑爆日志/响应头（h11 对头部字节数有限制）。
_MAX_REQUEST_ID_LEN = 128


class RequestIdMiddleware:
    """请求级 request_id 注入（纯 ASGI，两进程共用）。

    **为何不直接采信入站 ``X-Request-ID``**：CR/LF 等控制字符会造成日志伪造与响应头注入
    （h11 会直接拒答），非 ASCII 也会污染结构化日志。不合规即另生成一个并**全程使用自己
    生成的**（而不是回退到入站值），保证下游看到的一定是净化过的。

    写入 ContextVar 后，JSON 日志（``core.logging``）与响应信封
    （``core.err.resp_json`` / ``core.wire.msgspec_ok``）都能取到同一个 id，请求与其日志、
    错误体因此可互相检索。回写响应头让客户端也能拿到。
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


class GraphQLHTTPMiddleware:
    """GraphQL 端点的 HTTP 边缘关切（§2）：查询级**硬**超时 + 版本响应头。

    只对 ``settings.graphql_path`` 下的请求生效，其余请求零开销直通。

    **硬超时**（§2 第 3 条的后半句「HTTP 兜底超时」）：``app.api.graphql.GraphQLGuard`` 只能在
    resolver 边界检查预算，中断不了已在 ``await`` 中的协程（Python 无抢占式取消），故它覆盖
    不到的形态——协程卡死在 await 里——由这里用 ``asyncio.wait_for`` 兜底：超时即取消该 task，
    在飞的 DB 查询随取消一并掐断（读会话由 ``get_read_session`` 依赖托管，取消时其 ``finally``
    正常收尾），并向客户端回 504 信封。阈值 ``graphql_hard_timeout_s`` 必须显著宽于查询级
    ``graphql_timeout_s``，否则会抢先把后者的「HTTP 200 + errors」变成 504。

    **为何缓冲下游 ASGI 消息**：一旦把 ``http.response.start`` 转发出去就再也不能改主意
    回 504，故先缓冲、未超时才按序转发。GraphQL 响应体小（聚合读的投影结果），这点内存
    代价换到真正的墙钟上限是划算的。

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

        # 版本取**已匹配路由的路径模板**而非原始 URL，避免手写段解析与挂载方式漂移
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
        # assert_web_security_configured 只拦「空值」，LKM_ALLOWED_HOSTS=*（或 *,foo）能过闸；
        # 而 Starlette 见到 "*" 会置 allow_any=True 整体跳过校验——恰恰在生产把这道
        # 「挡绕过网关直连容器端口」的纵深防御静默关掉，故这里显式拒绝
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
    # request_id **最后加 → 这一组最外层**：连 CORS 预检、TrustedHost 拒答也带 X-Request-ID。
    # 必须比 app.main._log_requests 更外层，访问日志才读得到本次请求的 id（见该中间件）。
    application.add_middleware(RequestIdMiddleware)


__all__: list[str] = [
    "_HSTS_VALUE",
    "GraphQLHTTPMiddleware",
    "RequestIdMiddleware",
    "SecurityHeadersMiddleware",
    "install_security_middleware",
]
