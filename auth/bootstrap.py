"""auth 侧自注册：把 auth 的能力交给 core（注册表 / 路由 / 端口实现）。

装配根 ``boot.assemble`` 调用 :func:`register`。auth 因此无需被 app import，
app 也无需知道 auth 的存在——两边只对 core 说话。
"""

from __future__ import annotations

_registered = False


def register() -> None:
    """幂等注册：ORM 模型、Pulsar 任务、错误码、前台路由与全部端口实现。"""
    global _registered
    if _registered:
        return

    from auth import register as _register

    # 模型挂独立 AuthBase 元数据（业务库不含 auth 表），由 auth 自己 import + configure
    _register.register_models()
    _register.register_tasks()
    _register.register_errors()

    # 前台认证路由：登记进 core，供 app/api/router.py 装配时取用
    from auth import ROUTERS
    from core import route_registry

    route_registry.register_routers(ROUTERS)

    # core 端口实现（鉴权/快照/审计/用户运维/验签）
    from auth.ports_impl import bind_all

    bind_all()

    _registered = True
