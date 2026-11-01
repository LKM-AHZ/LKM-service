"""
消息总线传输实现。

- :mod:`base`      ``Transport`` 协议（publish 的唯一签名）
- :mod:`rabbitmq`  RabbitMQ 实现（发布 + 订阅 + 健康探测）
- :mod:`inmemory`  内存实现（测试/插件替身，经 ``set_transport`` 注入）

**Pulsar 不在本包内**：它持有 bus 级生命周期（client、producer 缓存、「已关闭」标记），
与 ``core.bus.runtime`` 同寿。把它也搬进来会把一个生命周期撕成两半、并迫使 closed 状态
在两处各存一份（两份状态必然漂移）。故 Pulsar 作为**内置默认实现**留在 runtime，
其余 broker 以本包的 ``Transport`` 接口接入。详见设计文档 ADR-004。
"""

from __future__ import annotations
