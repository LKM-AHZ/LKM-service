"""
通用消息总线（``core.bus``）。

按「数据 → 编码 → 契约 → 发布 → 消费」分层，替代原 760 行的 ``core/messaging.py``：

- :mod:`core.bus.topics`    逻辑 routing_key → topic、订阅清单、命名空间隔离校验（纯数据）
- :mod:`core.bus.schema`    envelope JSON Schema 与 JSON 编码
- :mod:`core.bus.contracts` 事件契约校验、永久失败判定
- :mod:`core.bus.runtime`   发布路径：编码 → 契约 → props → 选 transport → 投递
- :mod:`core.bus.consumer`  消费路径：订阅线程、ack/nack、超时

同事务的跨模块协作（L2）**不在本包**：由 :mod:`core.ports` 的端口契约承担——显式函数
签名、``db`` 由调用方传入、装配期 ``validate_all()`` fail-fast。早期曾有一个按字符串分发的
``core.bus.dispatcher`` 骨架，因始终无接线、且静态保证弱于端口，已删除（方案 ADR-007）。

历史入口 ``core.messaging`` 保留为 re-export 垫片，**不含任何实现**。
"""

from __future__ import annotations
