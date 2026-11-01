"""
通用消息总线（``core.bus``）。

按「数据 → 编码 → 契约 → 发布 → 消费」分层，替代原 760 行的 ``core/messaging.py``：

- :mod:`core.bus.topics`    逻辑 routing_key → topic、订阅清单、命名空间隔离校验（纯数据）
- :mod:`core.bus.schema`    envelope JSON Schema 与 JSON 编码
- :mod:`core.bus.contracts` 事件契约校验、永久失败判定
- :mod:`core.bus.runtime`   发布路径：编码 → 契约 → props → 选 transport → 投递
- :mod:`core.bus.consumer`  消费路径：订阅线程、ack/nack、超时
- :mod:`core.bus.dispatcher` L2 进程内命令/查询总线（同步、共享调用方 session）

历史入口 ``core.messaging`` 保留为 re-export 垫片，**不含任何实现**。
"""

from __future__ import annotations
