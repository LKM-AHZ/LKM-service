"""app 侧自注册：把业务模块的模型/任务、错误码、seed 步骤与自有端口实现登记进 core。

装配根 ``boot.assemble`` 调用 :func:`register`。core 因而不知道任何业务模块名，
app 也不需要 import auth。
"""

from __future__ import annotations

#: 需在装配期导入的 ORM 模型模块（全量 metadata → relationship 字符串解析）。
APP_MODEL_MODULES: tuple[str, ...] = (
    "app.modules.admin.models",
    "app.modules.content.articles.models",
    "app.modules.content.blog.models",
    "app.modules.content.models",
    "app.modules.exam.models",
    "app.modules.feed.models",
    "app.modules.files.models",
    "app.modules.interaction.models",
    "app.modules.notification.models",
    "app.modules.points.models",
    "app.modules.projects.models",
    "app.modules.starhope.models",
)

#: 需在装配期导入的 Pulsar 任务模块（导入即副作用注册 handler / cron）。
APP_TASK_MODULES: tuple[str, ...] = (
    "app.modules.content.blog.tasks",
    "app.modules.content.tasks",
    "app.modules.feed.tasks",
    "app.modules.files.tasks",
    "app.modules.interaction.tasks",
    "app.modules.notification.tasks",
    "app.modules.points.tasks",
    "app.modules.search.tasks",
)

_registered = False


class _ContentStatsImpl:
    """``core.ports.content_stats`` 的业务侧实现（core 不得知道业务表）。"""

    async def count_content_created_by_day(self, days: int) -> dict[str, int]:
        from app.modules.content.daily_stats import count_content_created_by_day

        return await count_content_created_by_day(days)


def register() -> None:
    """幂等注册：业务模型/任务、错误码、seed 步骤与 content_stats 端口。"""
    global _registered
    if _registered:
        return

    from core import ports, task_registry
    from core.db import init_db, model_registry

    for path in APP_MODEL_MODULES:
        model_registry.register_module(path)
    for path in APP_TASK_MODULES:
        task_registry.register_module(path)

    # 各业务模块错误码（导入即 register() 副作用）
    from app.modules import registry as _modules

    _modules.load_errors()

    # schema 就绪后种 RBAC 默认权限（原 core.db.init_db 直连业务 seed，改为登记）
    from app.modules.rbac.seed import seed_rbac

    init_db.register_seed_step(seed_rbac)  # type: ignore[arg-type]

    ports.install("content_stats", _ContentStatsImpl())

    _registered = True
