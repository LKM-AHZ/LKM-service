"""
业务模块清单：模块注册的**单一事实源**（蓝图 §7）。

新增一个业务域只需在此表加一条 :class:`ModuleSpec`；REST/GraphQL 聚合、错误码导入、
ORM 模型导入与任务注册四项都由本表派生。拆分前它们分散在 ``registry.MODULES``、
``registry._ERROR_MODULES`` 与 ``app.bootstrap`` 的两个元组里，改漏一处的表现是运行时
404 或缺错误码，而不是启动失败。

派生规则（刻意与拆分前的导入顺序逐项一致，见 ``tests/test_module_spec.py``）：

- ``MODULES``：``aggregate=True`` 的模块，按表序——顺序决定路由注册顺序；
- ``ERROR_MODULES``：先各模块顶层 ``errors.py``，再各模块声明的子包错误码模块；
- ``APP_MODEL_MODULES`` / ``APP_TASK_MODULES``：按表序展平。

auth 不在本表内：它已独立成顶层包，模型/错误码/任务由 ``auth.bootstrap`` 自行注册。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleSpec:
    """一个业务域在装配期的全部注册项。

    字段全部是「导入即产生副作用」的模块路径；``__init__.py`` 的公共 API
    （``ROUTERS`` / ``GRAPHQL``）不在此声明，由 ``registry`` 按名读取。
    """

    name: str
    #: 是否存在顶层 ``app.modules.<name>.errors``。admin 的错误码在
    #: ``admin.moderation`` 子包下；feed/treehole 的错误码已随功能迁移，health 无错误码。
    top_errors: bool = True
    #: 额外的错误码模块（子包路径），按表序展平在顶层错误码之后。
    extra_errors: tuple[str, ...] = ()
    #: ORM 模型模块（全量 metadata → relationship 字符串解析）。
    models: tuple[str, ...] = ()
    #: Pulsar 任务模块（导入即副作用注册 handler / cron）。
    tasks: tuple[str, ...] = ()
    #: 是否参与 REST/GraphQL 聚合。rbac 只提供跨模块权限原语，不导出路由/字段。
    aggregate: bool = True


SPECS: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        name="admin",
        top_errors=False,
        extra_errors=(
            # ModerationErr：错误码在子包下，非 app.modules.admin.errors
            "app.modules.admin.moderation",
        ),
        models=("app.modules.admin.models",),
        tasks=("app.modules.admin.tasks",),
    ),
    ModuleSpec(
        name="content",
        extra_errors=(
            # 内容子域各自的错误码（并入 content 后不再由 MODULES 派生，须单列）
            "app.modules.content.articles",
            "app.modules.content.blog",
            "app.modules.content.boards",
            "app.modules.content.columns",
            "app.modules.content.qa",
        ),
        models=(
            "app.modules.content.articles.models",
            "app.modules.content.blog.models",
            "app.modules.content.models",
        ),
        tasks=(
            "app.modules.content.blog.tasks",
            "app.modules.content.tasks",
        ),
    ),
    ModuleSpec(
        name="exam",
        models=("app.modules.exam.models",),
    ),
    ModuleSpec(
        name="feed",
        top_errors=False,
        models=("app.modules.feed.models",),
        tasks=("app.modules.feed.tasks",),
    ),
    ModuleSpec(
        name="files",
        models=("app.modules.files.models",),
        tasks=("app.modules.files.tasks",),
    ),
    ModuleSpec(
        name="health",
        top_errors=False,
    ),
    ModuleSpec(
        name="interaction",
        models=("app.modules.interaction.models",),
        tasks=("app.modules.interaction.tasks",),
    ),
    ModuleSpec(
        name="notification",
        models=("app.modules.notification.models",),
        tasks=("app.modules.notification.tasks",),
    ),
    ModuleSpec(
        name="points",
        models=("app.modules.points.models",),
        tasks=("app.modules.points.tasks",),
    ),
    ModuleSpec(
        name="projects",
        models=("app.modules.projects.models",),
    ),
    ModuleSpec(
        name="search",
        tasks=("app.modules.search.tasks",),
    ),
    ModuleSpec(
        name="starhope",
        models=("app.modules.starhope.models",),
    ),
    ModuleSpec(
        name="treehole",
        top_errors=False,
        models=("app.modules.treehole.models",),
    ),
    ModuleSpec(
        name="rbac",
        aggregate=False,
    ),
)

#: 参与 REST/GraphQL 聚合的模块名，按表序（顺序即路由注册顺序）。
MODULES: tuple[str, ...] = tuple(s.name for s in SPECS if s.aggregate)

#: 需在装配期导入以触发 ``register()`` 的错误码模块，按表序。
ERROR_MODULES: tuple[str, ...] = (
    *(f"app.modules.{s.name}.errors" for s in SPECS if s.aggregate and s.top_errors),
    *(m for s in SPECS if s.aggregate for m in s.extra_errors),
)

#: 需在装配期导入的 ORM 模型模块（全量 metadata）。
APP_MODEL_MODULES: tuple[str, ...] = tuple(m for s in SPECS for m in s.models)

#: 需在装配期导入的 Pulsar 任务模块（导入即副作用注册）。
APP_TASK_MODULES: tuple[str, ...] = tuple(m for s in SPECS for m in s.tasks)
