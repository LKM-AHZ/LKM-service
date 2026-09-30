"""客户端真实 IP 解析：网关后用 ``X-Real-IP``，直连回落 peer 地址（M6 修复）。

**修的是什么**：此前各处直接用 ``request.client.host``。自 M5 7.2.4 换 APISIX 网关后，
应用看到的 peer 是 **apisix 容器的 IP**，于是所有「按 IP」的限流退化成**全站共享一个桶**
——`LKM_LOGIN_IP_MAX_PER_MIN`（20/分）实际变成"全站 20 次/分"，正常用户会被误锁，
且按 IP 归因的审计也记的是网关地址。

**为什么可以信这个头**：APISIX 的 global rule（``deploy/apisix/apisix.yaml``）用
``$remote_addr`` 以 ``set`` 语义**覆写**（非追加）``X-Real-IP``/``X-Forwarded-For``，
故经网关到达时该值不可由客户端伪造；而 backend/auth/astro 在 compose 里**均无对外端口**
（只暴露 apisix 的 80/443），生产流量必经网关，不存在绕过网关直连注入该头的路径。

直连场景（本地开发、ASGITransport 测试、容器内 healthcheck/探针）没有该头 → 回落
``request.client.host``，行为与改动前一致。
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import Request

logger = logging.getLogger("lkm.client_ip")

# 网关注入的真实客户端 IP 头名。语义为「单值、由边缘覆写」，**不解析**其追加链。
REAL_IP_HEADER = "X-Real-IP"

# 取不到时的占位值。刻意不用空串：``f"ip:{''}"`` 会让"取不到 IP"的所有请求共用一个桶，
# 与"某个真实 IP"混淆；显式 ``unknown`` 让该桶名可被识别与告警。
UNKNOWN_IP = "unknown"


def client_ip(request: Request) -> str:
    """取真实客户端 IP：优先网关注入的 ``X-Real-IP``，否则回落直连 peer 地址。

    取不到时返回 :data:`UNKNOWN_IP`（见其注释说明为何不用空串）。
    """
    forwarded = (request.headers.get(REAL_IP_HEADER) or "").strip()
    if forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            # 不是 IP 字面量（逗号拼接链 / 注入内容 / 超长串）：不信这个头，回落 peer。
            # 上面的信任前提是两条部署不变式（网关 set 覆写、后端无对外端口）；一旦被破坏
            # （dev compose 误发布后端端口、前置代理改成 append），该值即为攻击者可控——
            # 直接透传会污染按 IP 的限流桶与审计日志。
            logger.warning("X-Real-IP 非法，忽略并回落 peer：%r", forwarded[:64])
    return request.client.host if request.client else UNKNOWN_IP


__all__ = ["REAL_IP_HEADER", "UNKNOWN_IP", "client_ip"]
