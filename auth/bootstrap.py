"""auth 侧自注册：把 auth 的能力交给 core（注册表 / 端口实现）。

装配根 ``boot.assemble`` 调用 :func:`register`。auth 因此无需被 app import，
app 也无需知道 auth 的存在——两边只对 core 说话。

**不再向单体登记 auth 路由**：双形态已退役，认证 HTTP 面只由 ``auth.main`` 提供；
单体只通过 ``core.ports`` 经 HTTP 缝调用 auth。
"""

from __future__ import annotations

_registered = False


def register() -> None:
    """幂等注册：ORM 模型、Pulsar 任务、错误码与全部端口实现。"""
    global _registered
    if _registered:
        return

    from auth import register as _register

    # 模型挂独立 AuthBase 元数据（业务库不含 auth 表），由 auth 自己 import + configure
    _register.register_models()
    _register.register_tasks()
    _register.register_errors()

    # core 端口实现（鉴权/快照/审计/用户运维/验签）
    from auth.ports_impl import bind_all

    bind_all()

    _registered = True
