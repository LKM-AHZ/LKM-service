"""测试替身：内存消息总线 transport 等。

默认测试套件不依赖真实 Pulsar。经 ``messaging.set_transport(InMemoryTransport())`` 注入后，
``messaging.publish`` 走内存记录而非 broker，可直接断言发布的 topic/属性/负载。
"""

from __future__ import annotations

import json
from typing import Any


class InMemoryTransport:
    """记录发布的内存 transport（结构匹配 messaging.Transport 协议）。

    ``fail=True`` 时 ``publish`` 抛错，用于验证 fail-open 与失败计数。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.fail = fail

    async def publish(self, topic: str, data: bytes, props: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("in-memory transport failure")
        self.published.append((topic, data, props))

    def payloads(self) -> list[dict[str, Any]]:
        """已发布消息的 JSON 负载列表。"""
        return [json.loads(data) for _, data, _ in self.published]

    def clear(self) -> None:
        self.published.clear()
