"""进程装配：把 app 与 auth 各自的注册项汇总进 core，并校验端口齐备。

**唯一允许同时 import app 与 auth 的地方。** app 与 auth 彼此零 import，因此「谁在何时
把两侧装到一起」必须由组装层回答——就是这里。

幂等：同一进程内重复调用只执行一次（``_assembled``）。测试可在会话级 fixture 调一次。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("lkm.boot")

_assembled = False


def _import_plugins() -> None:
    """按清单顺序 import 每个插件的入口包（``module``），失败时报出插件名。

    这是非 aggregate 插件唯一的初始化钩子：它们没有 REST/GraphQL 出口，主模块不会被
    路由聚合顺带 import。插件在导入期调用 core 注册表完成自注册（``ports.install``、
    ``init_db.register_seed_step``、``task_registry.register_task``、错误码等）。
    聚合模块随后也会被 import，Python 的模块缓存保证副作用只执行一次。

    顺序 = ``spec.SPECS`` 的顺序（由 manifest 依赖图定序），因此 ``requires`` 可以约束
    「先让另一个插件完成注册，再执行本插件的注册」。
    """
    from importlib import import_module

    from app.modules.spec import SPECS

    for spec in SPECS:
        try:
            import_module(spec.module_path)
        except Exception as exc:
            raise RuntimeError(
                f"插件 {spec.name!r} 的入口 {spec.module_path!r} 加载失败：{exc}"
            ) from exc


def assemble() -> None:
    """登记两侧注册项 → 加载插件入口 → 导入全量模型/任务 → 校验端口齐备。"""
    global _assembled
    if _assembled:
        return

    import app.bootstrap
    import auth.bootstrap

    app.bootstrap.register()
    auth.bootstrap.register()
    _import_plugins()

    import boot.plugins

    boot.plugins.register_hooks()

    from core.db.model_registry import ensure_all_models
    from core.task_registry import import_task_modules

    ensure_all_models()
    import_task_modules()

    # 缺端口即启动失败：鉴权类端口若未绑定就静默放行，是本重构最危险的失效模式
    from core import ports

    ports.validate_all()
    _assembled = True
    logger.debug("boot.assemble 完成：模型/任务/端口均已就绪")


def is_assembled() -> bool:
    return _assembled


def reset() -> None:
    """重置装配标志（仅供测试隔离；不清空注册表，注册项本身幂等）。"""
    global _assembled
    _assembled = False
