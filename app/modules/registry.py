"""模块注册表（§7）：业务模块的单一事实源与聚合入口。

新增业务域 = 在这里的 ``MODULES`` 加一行 + 建模块目录；REST/GraphQL/错误码/任务
聚合均由本表驱动，框架文件（api/router、api/graphql、main）零改动。

跨模块 import 走各模块 ``__init__.py`` 的公共 API；本表用 ``import_module`` 延迟
加载（字符串导入），不产生 ``main`` 命名空间对 ``app`` 包名的绑定冲突（见 §7）。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# 唯一事实源：业务模块清单（顺序即聚合顺序）。新增模块在此登记。
MODULES: list[str] = [
    "admin",
    "articles",
    "auth",
    "blog",
    "content",
    "exam",
    "feed",
    "files",
    "health",
    "interaction",
    "points",
    "projects",
    "starhope",
    # rbac 无 REST/GraphQL 导出，但承载跨模块权限框架，无需在此列表聚合路由；
    # 若其注册了错误码/依赖副作用需要随应用加载，可加入并自行判定 hasattr。
]

# 注册副作用需要显式导入其 errors 的模块（rbac 无 errors，故单独列出）。
# 注：各模块错误码通过 ``register()`` 副作用注册，导入即生效。
_ERROR_MODULES: list[str] = [
    "articles",
    "auth",
    "blog",
    "content",  # ContentErr（统一内容核心）
    "content.boards",  # BoardErr
    "content.columns",  # ColumnErr
    "content.qa",  # QaErr
    "exam",
    "feed",  # FollowErr（关注关系；M2.3 原 follow 域并入）
    "files",
    "interaction",
    "points",
    "projects",
    "starhope",
]


def each_module(name: str) -> Any:
    """延迟 import 并返回模块对象（用字符串导入避免 ``app`` 包名绑定）。"""
    return import_module(f"app.modules.{name}")


def routers_of(name: str) -> list[Any]:
    """模块在 __init__.py 暴露的 ``ROUTERS`` 列表（可空）。"""
    return list(getattr(each_module(name), "ROUTERS", []) or [])


def graphql_of(name: str) -> list[Any]:
    """模块在 __init__.py 暴露的 ``GRAPHQL`` 列表（可空）。"""
    return list(getattr(each_module(name), "GRAPHQL", []) or [])


def load_errors() -> None:
    """导入各模块 ``errors`` 模块触发错误码 register() 副作用（幂等）。"""
    for name in _ERROR_MODULES:
        import_module(f"app.modules.{name}.errors")


def load_all() -> None:
    """应用/worker 装配入口：加载全部模块并触发注册副作用。

    聚合：错误码（load_errors）。模型与任务的预注册由各自基础设施枢纽
    （db.model_registry / core.task_registry）承担，此处聚焦业务侧副作用，
    避免重复触发；如需随应用启动一并注册，调用方按需组合。
    """
    load_errors()
