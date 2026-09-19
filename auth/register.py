"""auth 包对 app 侧的注册钩子：模型 / 任务 / 错误码。

app 侧的基础设施枢纽（``app.db.model_registry``、``app.core.task_registry``、
``app.modules.registry``）不得直接 import auth 内部模块，只调这里的函数触发注册副作用；
实现一律惰性 import，保持 ``import auth`` 轻量、无包级循环。
"""

from __future__ import annotations


def register_models() -> None:
    """注册 auth ORM 模型并锁定 auth registry 配置（幂等）。

    导入 ``auth.models`` 使 ``AuthBase.metadata`` 填满，随后 ``configure()`` 解析
    relationship 字符串引用；重复调用由 registry 的 ``_configured`` 标志保护。
    """
    import auth.models  # noqa: F401
    from auth.db.base import AuthBase

    if not getattr(AuthBase.registry, "_configured", False):
        AuthBase.registry.configure()


def register_tasks() -> None:
    """导入 ``auth.tasks`` 触发 Pulsar handler / cron 声明注册（幂等）。"""
    import auth.tasks  # noqa: F401


def register_errors() -> None:
    """导入 ``auth.errors`` 触发错误码 ``register()`` 副作用（幂等）。"""
    import auth.errors  # noqa: F401
