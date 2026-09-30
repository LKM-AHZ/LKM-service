"""模块注册表（§7）：业务模块的单一事实源与聚合入口。

新增业务域 = 在这里的 ``MODULES`` 加一行 + 建模块目录；REST/GraphQL/错误码/任务
聚合均由本表驱动，框架文件（api/router、api/graphql、main）零改动。
表的范围是 ``app/modules/`` 下的**业务域**；auth 已独立成顶层包（``auth/``），
不在此表内——其 REST 面由 ``api/router.py`` 显式挂载，错误码经 ``auth.register_errors()``
注册，模型/任务经 ``auth.register_models()`` / ``auth.register_tasks()`` 注册。

跨模块 import 走各模块 ``__init__.py`` 的公共 API；本表用 ``import_module`` 延迟
加载（字符串导入），不产生 ``main`` 命名空间对 ``app`` 包名的绑定冲突（见 §7）。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# 唯一事实源：业务模块清单（顺序即聚合顺序）。新增模块在此登记。
# **articles / blog 已并入 content 聚合（蓝图 M2「内容域收敛」）**：它们不再是独立业务域，
# 而是 `app.modules.content` 下的子包（与 boards/columns/qa 同构，路由前缀与表结构不变
# ——M2 验收明许「过渡期可目录合并+路由拆片」）。故从本表移除、由 content 聚合二者的
# ROUTERS/GRAPHQL；跨模块 import 因此归零。
MODULES: tuple[str, ...] = (
    "admin",
    "content",
    "exam",
    "feed",
    "files",
    "health",
    "interaction",
    "notification",
    "points",
    "projects",
    "search",
    "starhope",
    # rbac 无 REST/GraphQL 导出，但承载跨模块权限框架，无需在此列表聚合路由；
    # 若其注册了错误码/依赖副作用需要随应用加载，可加入并自行判定 hasattr。
)

# 不做顶层 errors.py 的模块：admin 的错误码在子包 admin.moderation 下；health 无错误码；
# feed 的错误码（FollowErr）随关注关系迁入 interaction 后已无自有错误码。
_NO_TOP_ERRORS: frozenset[str] = frozenset({"admin", "feed", "health"})

# 注册副作用需要显式导入其 errors 的模块。各模块错误码通过 ``register()`` 副作用注册，
# 导入即生效。这里**由 MODULES 派生**而非另抄一份清单：两份手维护清单必然漂移（此前
# "storage" 就漏了，只靠 files/auth 的 import 链顺带注册；admin 的错误码在子包
# admin.moderation 下，按约定路径 app.modules.admin.errors 根本导不到，只能单列）。
_ERROR_MODULES: list[str] = [
    *(m for m in MODULES if m not in _NO_TOP_ERRORS),
    "admin.moderation",  # ModerationErr（子包路径，非 app.modules.admin.errors）
    "content.articles",  # ArticleErr：并入 content 后不再由 MODULES 派生，须单列
    "content.blog",  # BlogErr：同上
    "content.boards",  # BoardErr
    "content.columns",  # ColumnErr
    "content.qa",  # QaErr
    # 注：StorageErr 随存储抽象下沉 core.storage，不在业务错误码清单内——
    # 其注册由 core.storage.errors 自身在导入期完成（见 load_errors）。
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
    # 存储抽象已下沉 core（auth 与 files 共用），其错误码不属任何业务模块
    import_module("core.storage.errors")


def load_all() -> None:
    """应用/worker 装配入口：加载全部业务模块并触发注册副作用。

    只负责**业务侧**错误码（load_errors）；auth 的错误码/模型/任务由其 bootstrap 自行注册
    （``auth.bootstrap.register``，由 ``boot.assemble`` 触发）——app 不 import auth。
    模型与任务的预注册由 ``core.db.model_registry`` / ``core.task_registry`` 承担。
    """
    load_errors()
