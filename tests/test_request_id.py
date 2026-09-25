"""请求级 request_id 贯通（蓝图 §6.1 信封 ``{code, data, message, request_id}``）。

request_id 的生成/净化原先只活在 ``app.main._log_requests``，auth 独立进程**完全没有**对应
中间件 → 它的响应既无 ``X-Request-ID`` 头也没有日志关联。现收拢到
``core.middleware.RequestIdMiddleware``（两进程共用 ``install_security_middleware`` 装配）。

本文件守四件事：头与信封同值、入站 id 被采纳、不安全的入站 id 被换掉、auth 进程同样生效。
"""

from __future__ import annotations

from typing import Any

# 需登录的读端点：未带凭据 → BizError → resp_json 信封（无需任何测试装置）。
_DENIED_PATH = "/api/v1/users/me/following"


async def test_envelope_request_id_matches_header(client: Any) -> None:
    """信封里的 request_id 必须与响应头 X-Request-ID 是同一个值。

    这是该字段的全部价值：前端把 body.request_id 报上来，就能直接检索后端日志。
    """
    resp = await client.get(_DENIED_PATH)

    rid = resp.headers.get("X-Request-ID")
    assert rid
    assert resp.json()["request_id"] == rid


async def test_inbound_request_id_is_adopted(client: Any) -> None:
    """网关/上游传下来的 X-Request-ID 要原样采纳（跨服务链路才能串起来）。"""
    resp = await client.get(_DENIED_PATH, headers={"X-Request-ID": "trace-abc-123"})

    assert resp.headers["X-Request-ID"] == "trace-abc-123"
    assert resp.json()["request_id"] == "trace-abc-123"


async def test_unsafe_inbound_request_id_is_replaced(client: Any) -> None:
    """超长/非 ASCII 的入站 id 不可采信：会撑爆日志与响应头（h11 对头部有限制）。"""
    resp = await client.get(_DENIED_PATH, headers={"X-Request-ID": "a" * 200})

    rid = resp.headers["X-Request-ID"]
    assert rid != "a" * 200
    assert len(rid) == 32  # uuid4().hex


async def test_auth_process_also_sets_request_id(auth_app_client: Any) -> None:
    """auth 独立进程此前完全没有 request-id 中间件——其响应无头、日志无关联。

    断言用必然失败（未认证）的 admin 刷新端点：错误路径同样要有 id，否则最需要排查的
    报错请求恰恰对不上日志。
    """
    resp = await auth_app_client.post("/api/v1/admin/auth/refresh")

    rid = resp.headers.get("X-Request-ID")
    assert rid
    assert resp.json()["request_id"] == rid
    assert resp.json()["message"]  # 信封字段名已随蓝图改名为 message
