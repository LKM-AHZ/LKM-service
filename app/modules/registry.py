"""模块注册表（§7）：业务模块的聚合入口。

**注册项的唯一事实源是** :mod:`app.modules.spec`——本模块只负责按清单派生出的路径做
延迟导入与聚合，不再各自维护 ``MODULES`` / ``_ERROR_MODULES`` 两份字面量。

表的范围是 ``app/modules/`` 下的**业务域**；auth 已独立成顶层包（``auth/``），
不在此表内，也不在业务 app 上挂路由。``boot.assemble`` 调用两侧 bootstrap 完成装配，
app 只经 ``core.ports`` 与 auth 协作。

跨模块 import 走各模块 ``__init__.py`` 的公共 API；本表用 ``import_module`` 延迟
加载（字符串导入），不产生 ``main`` 命名空间对 ``app`` 包名的绑定冲突（见 §7）。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.modules.spec import ERROR_MODULES, MODULE_PATHS, MODULES

__all__ = [
    "ERROR_MODULES",
    "MODULES",
    "MODULE_PATHS",
    "each_module",
    "graphql_of",
    "load_all",
    "load_errors",
    "routers_of",
]


def each_module(name: str) -> Any:
    """延迟 import 并返回模块对象（用字符串导入避免 ``app`` 包名绑定）。

    导入路径取自清单：内置模块是 ``app.modules.<name>``，外部插件是发行版声明的路径。
    """
    try:
        path = MODULE_PATHS[name]
    except KeyError:
        raise ValueError(f"未登记的模块 {name!r}（不在 manifest 清单内）") from None
    return import_module(path)


def routers_of(name: str) -> list[Any]:
    """模块在 __init__.py 暴露的 ``ROUTERS`` 列表（可空）。"""
    return list(getattr(each_module(name), "ROUTERS", []) or [])


def graphql_of(name: str) -> list[Any]:
    """模块在 __init__.py 暴露的 ``GRAPHQL`` 列表（可空）。"""
    return list(getattr(each_module(name), "GRAPHQL", []) or [])


def load_errors() -> None:
    """导入各模块错误码模块触发 ``register()`` 副作用（幂等）。"""
    for path in ERROR_MODULES:
        import_module(path)
    # 存储抽象已下沉 core（auth 与 files 共用），其错误码不属任何业务模块
    import_module("core.storage.errors")


def load_all() -> None:
    """应用/worker 装配入口：加载全部业务模块并触发注册副作用。

    只负责**业务侧**错误码（load_errors）；auth 的错误码/模型/任务由其 bootstrap 自行注册
    （``auth.bootstrap.register``，由 ``boot.assemble`` 触发）——app 不 import auth。
    模型与任务的预注册由 ``core.db.model_registry`` / ``core.task_registry`` 承担。
    """
    load_errors()
