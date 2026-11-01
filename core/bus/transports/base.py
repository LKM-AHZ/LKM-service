"""
传输协议：发布侧唯一需要实现的方法。

签名刻意只收 ``(topic, data, props)``：``data`` 是**已编码**的线上字节，
传输实现不得再改编码格式（编码口径由 ``core.bus.schema._encode_event`` 统一）。
"""

from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    """发布 transport。``set_transport`` 注入的替身与真实 broker 实现共用此签名。"""

    async def publish(self, topic: str, data: bytes, props: dict[str, str]) -> None: ...
