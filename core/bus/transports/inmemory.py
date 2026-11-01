"""
内存 transport：记录发布内容、不连 broker。

用途：单元测试经 ``set_transport(InMemoryTransport())`` 注入后，断言「发布了什么」而无需
真实 broker；也用于需要离线跑通发布路径的插件自检。
"""

from __future__ import annotations

import json
from typing import Any


class InMemoryTransport:
    """记录发布的内存 transport（结构匹配 :class:`core.bus.transports.base.Transport`）。

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
        """已发布消息的 JSON 负载列表。

        空/非 JSON 负载通常说明该用例在验证裸传输契约（或发布方写坏了）：给出带 topic 与
        原始字节的明确断言信息，而不是从 json.loads 冒一个无上下文的 JSONDecodeError。
        """
        out: list[dict[str, Any]] = []
        for topic, data, _ in self.published:
            try:
                out.append(json.loads(data))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise AssertionError(
                    f"topic={topic!r} 的已发布负载不是合法 JSON"
                    f"（{len(data)}B）：{data[:80]!r}"
                ) from exc
        return out

    def clear(self) -> None:
        self.published.clear()
