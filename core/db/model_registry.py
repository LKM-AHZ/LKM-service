"""模型预注册中心：导入各模块 ``models.py``，供 SQLAlchemy registry 解析字符串关系/外键。

历史角色由早年的巨型 ``models.py`` 承担（单文件 import 即带出全部模型）。模型归位后各模型
分散到各模块 ``models.py``，本模块作为"导入枢纽"——任何需要全量业务模型注册的入口
（init_db/create_all、worker 进程、Alembic env）只要
``from core.db.model_registry import ensure_all_models`` 即可。

**core 不知道任何业务模块名**：业务侧由 ``app.bootstrap`` 经 :func:`register_module` 登记
字符串路径，装配根（``boot.assemble``）统一触发；auth 模型挂独立 ``AuthBase`` 元数据，
由 ``auth.bootstrap`` 自行 import + configure，不经本模块。

``Base.registry.configure()`` 必须在全部模型注册后调用，使 relationship 字符串引用得以解析。
重复调用是安全的（``mapperlib._configure_registries`` 在无新增 mapper 时直接返回），
故这里不做守卫——既不再依赖 SQLAlchemy 私有属性，也顺带覆盖「之后又注册了新模型」的情况。
"""

from __future__ import annotations

import importlib

import core.db.base as _base_module

#: core 自己的表（业务库内的共享基础设施表），无需外部登记。
_CORE_MODEL_MODULES: tuple[str, ...] = (
    "core.db.outbox",
    "core.db.outbox_archive",
    "core.db.event_processed",
    "core.db.event_failure",
    "core.db.user_dim",
    "core.db.dlq",
)

#: 由各顶层包 bootstrap 登记的模型模块路径（字符串，避免 core 依赖业务模块名）。
_MODEL_MODULES: list[str] = []


def register_module(path: str) -> None:
    """登记一个需在装配期导入的 ``models`` 模块（幂等；重复登记只保留一份）。"""
    if path not in _MODEL_MODULES:
        _MODEL_MODULES.append(path)


def ensure_all_models() -> None:
    """预注册全部 ORM 模型并完成 mapper 配置（幂等）。

    副作用：import core 自身表与全部已登记业务模块的 models.py，使 metadata 填满、
    relationship 字符串引用得以解析。随后 configure() 锁定配置。
    """
    for path in (*_CORE_MODEL_MODULES, *_MODEL_MODULES):
        importlib.import_module(path)

    # 不用 getattr(registry, "_configured", False) 做幂等守卫：那是 SQLAlchemy 无稳定性
    # 承诺的私有状态（实测本版根本没有该属性，守卫早已退化成「每次都调」）。configure()
    # 本身在「无新增 mapper」时是 no-op，直接调用最稳，也不依赖内部实现。
    _base_module.Base.registry.configure()
