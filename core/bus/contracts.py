"""
事件契约校验：发布期与消费期共用的唯一判据。

分工：
- ``event_contract``（``core/event_contract.py``）声明「逻辑 routing_key → 允许的 fn」与
  实参个数/类型，并按线上形态校验；
- 本模块把该判据套到**线上 envelope**上，并给出「是否属永久失败」的结论，供 outbox relay
  决定重试还是折叠、供 DLQ 归属判定。

两个入口都必须**延迟** import ``core.event_contract``：它反向 import 本包的 routing_key
常量，模块级互相 import 会成环（放函数里只是 ``sys.modules`` 查表，成本可忽略）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.bus.schema import _encode_event
from core.bus.topics import ROUTING_KEY_TOPICS


def _contract_violations(payload: Any, routing_key: str | None) -> list[str]:
    """
    事件契约校验的延迟入口。
    """
    from core import event_contract

    return event_contract.payload_violations(payload, routing_key)


def _record_violation(fn: Any, side: str, problems: list[str], *, topic: str) -> None:
    """记一次契约违约（延迟 import，理由同 :func:`_contract_violations`）。"""
    from core import event_contract

    event_contract.record_violation(fn, side, problems, where=f"topic={topic}")


def contract_violations(routing_key: str, payload: Any) -> list[str]:
    """
    按**线上形态**校验 envelope 的事件契约；返回违约原因清单（空=合规）。
    与 :func:`permanent_failure_reason` 共用同一判据。独立暴露是为了让 relay 在**折叠**这类
    事件时能拿到结构化原因去记违约指标，而不必去解析原因字符串。
    """
    try:
        wire = json.loads(_encode_event(dict(payload)))
    except (TypeError, ValueError):
        return []  # 编码失败是另一类永久失败，由 permanent_failure_reason 单独报
    return _contract_violations(wire, routing_key)


def permanent_failure_reason(
    routing_key: str, payload: Mapping[str, Any]
) -> str | None:
    """
    判定一次发布是否属**永久失败**（重试无意义）；返回原因字符串，None = 可重试。
    只覆盖 relay 侧在投递前就可确定的确定性错误：
    - 未知 ``routing_key``：不在 ``ROUTING_KEY_TOPICS`` 里，连目标 topic 都定不出来；
    - payload 无法按线上格式编码（含非 JSON 可序列化对象）；
    - **事件契约违约**（``event_contract.payload_violations``，见该模块头）：fn 未登记、
      实参个数/类型不符、经不允许的 routing_key 承载等。
    其余情况（连接失败 / 超时 / 总线不可达 / broker 拒收）一律**不**判永久
    """
    if routing_key not in ROUTING_KEY_TOPICS:
        return f"unknown routing_key={routing_key}"
    try:
        _encode_event(dict(payload))
    except (TypeError, ValueError) as exc:
        return f"payload not json-encodable: {exc}"
    problems = contract_violations(routing_key, payload)
    if problems:
        return "event contract violation: " + "; ".join(problems)
    return None
