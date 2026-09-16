"""M6 客户端真实 IP 解析验收：网关后必须取 ``X-Real-IP``，而非 apisix 容器的 peer 地址。

回归守卫：此前各处直接用 ``request.client.host``，换 APISIX 后该值恒为网关容器 IP，
令「按 IP」限流退化成全站共享单桶（正常用户被误锁）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.client_ip import UNKNOWN_IP, client_ip

_app = FastAPI()


@_app.get("/ip")
async def _echo_ip(request: Request) -> dict[str, str]:
    return {"ip": client_ip(request)}


async def _call(headers: dict[str, str] | None = None, client: tuple | None = None) -> str:
    transport = ASGITransport(app=_app, client=client)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/ip", headers=headers or {})
    return resp.json()["ip"]


async def test_prefers_real_ip_header_over_peer() -> None:
    """网关注入 X-Real-IP 时以它为准——peer 是 apisix 容器地址，不可用。"""
    ip = await _call({"X-Real-IP": "203.0.113.7"}, client=("172.18.0.5", 1234))
    assert ip == "203.0.113.7"


async def test_falls_back_to_peer_when_header_absent() -> None:
    """直连（本地开发 / 测试 / 容器内探活）无该头 → 回落 peer，行为与改动前一致。"""
    ip = await _call(None, client=("198.51.100.9", 1234))
    assert ip == "198.51.100.9"


async def test_blank_header_falls_back_to_peer() -> None:
    """空/全空白头不算数（避免把 'ip:' 与空串混作一桶）。"""
    assert await _call({"X-Real-IP": "   "}, client=("198.51.100.9", 1)) == "198.51.100.9"


async def test_unknown_when_nothing_available() -> None:
    assert await _call(None, client=None) == UNKNOWN_IP


async def test_header_value_is_stripped() -> None:
    assert await _call({"X-Real-IP": " 203.0.113.7 "}, client=("1.2.3.4", 1)) == "203.0.113.7"


async def test_does_not_read_forwarded_for_chain() -> None:
    """只认 X-Real-IP：XFF 可能是客户端可追加的链，刻意不解析（网关用 set 覆写两者）。"""
    ip = await _call(
        {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}, client=("172.18.0.5", 1)
    )
    assert ip == "172.18.0.5"
